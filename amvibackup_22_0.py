#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AmviBackUp - local, versioned file backup for Windows.

Copyright (c) 2026 Maxim Dubkov (AmviSoft)
Licensed under the MIT License; see legal/LICENSE.txt.

Requirements: Python 3.12+ and PySide6.
"""
from __future__ import annotations

import argparse
import re
import errno
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
import platform
import subprocess
import textwrap
try:
    import winreg
except ImportError:
    winreg = None
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    from PySide6.QtCore import QObject, QThread, Qt, Signal, QTimer, QUrl, QEvent, QLockFile, Slot, QRect, QSize, QPoint
    from PySide6.QtGui import QCursor, QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QDragLeaveEvent, QIcon, QKeySequence, QShortcut
    from PySide6.QtWidgets import QListWidget, QToolTip, QStackedWidget, QRadioButton, QTreeWidget, QTreeWidgetItem, QButtonGroup
    from PySide6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QLayout,
        QMainWindow,
        QMenu,
        QSystemTrayIcon,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QAbstractSpinBox,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    print(
        "AmviBackUp requires PySide6. Install it with: py -m pip install PySide6",
        file=sys.stderr,
    )
    raise SystemExit(1)

_QT_NOISE_PREFIXES = (
    "QFontDatabase: Cannot find font directory",
    "Note that Qt no longer ships fonts",
)


def _qt_message_filter(_mode: Any, _context: Any, message: str) -> None:
    """Hides the harmless Qt font-directory notice; every other Qt message stays visible."""
    if message.startswith(_QT_NOISE_PREFIXES):
        return
    print(message, file=sys.stderr)


try:
    from PySide6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(_qt_message_filter)
except Exception:
    pass

APP_NAME = "AmviBackUp"
APP_VERSION = "22.0-beta"
APP_AUTHOR = "Maxim Dubkov (AmviSoft)"
APP_COPYRIGHT_YEAR = "2026"
APP_COPYRIGHT = f"\u00a9 {APP_COPYRIGHT_YEAR} {APP_AUTHOR}"
LEGAL_FOLDER_NAME = "legal"
APP_LICENSE_NAME = "MIT License"
RESTORE_FOLDER_NAME = "AmviBackUp restored"
SETTING_LAST_RESTORE_FOLDER = "restore.last_folder"
RESTORE_USER_ERROR_CODES = frozenset({
    "RESTORE_DESTINATION_INVALID",
    "RESTORE_DESTINATION_NOT_FOLDER",
    "RESTORE_DESTINATION_REDIRECT",
    "RESTORE_DESTINATION_IN_STORAGE",
    "RESTORE_DESTINATION_IN_SOURCE",
    "RESTORE_DESTINATION_MISSING",
    "RESTORE_NOTHING_SELECTED",
    "RESTORE_FOLDER_ONLY",
})
INSTALLER_MUTEX_NAME = "AmviBackUpAppMutex"
INSTALLER_LANGUAGE_FILE = "installer-language.txt"
SUPPORT_FIRST_RUN_FILE = "support-first-run.txt"
SETTING_SUPPORT_FIRST_RUN_PENDING = "ui.support_first_run_pending"
SETTING_SUPPORT_FIRST_RUN_SHOWN = "ui.support_first_run_shown"
DB_SCHEMA_VERSION = 13
DEFAULT_KEEP_LAST_N = 5
MIN_KEEP_LAST_N = 1
BACKUP_DEFER_DELAY_MS = 250
BACKUP_DEFER_MAX_ATTEMPTS = 40
MAX_KEEP_LAST_N = 1000
RETENTION_MODE_COUNT = "count"
RETENTION_MODE_TIME = "time"
DEFAULT_RETENTION_DAILY = 7
DEFAULT_RETENTION_WEEKLY = 4
DEFAULT_RETENTION_MONTHLY = 6
MAX_RETENTION_PERIODS = 1000
RETENTION_FUTURE_TOLERANCE_SECONDS = 300
SETTING_RETENTION_CLOCK_BLOCKED = "retention.clock_blocked"
SETTING_RETENTION_CLOCK_WALL_UTC_MS = "retention.clock_wall_utc_ms"
SETTING_RETENTION_CLOCK_TIMEZONE = "retention.clock_timezone"
RETENTION_CLOCK_JUMP_SECONDS = 120
RETENTION_CLOCK_LONG_GAP_DAYS = 30
RETENTION_CLOCK_CHECKPOINT_SECONDS = 3600
DEFAULT_CHANGE_DEBOUNCE_SECONDS = 5
MIN_CHANGE_DEBOUNCE_SECONDS = 1
MAX_CHANGE_DEBOUNCE_SECONDS = 3600

DEFAULT_MIN_AUTO_INTERVAL_MINUTES = 10
MAX_CHANGE_THRESHOLD_BYTES = 1 << 40
CHANGE_THRESHOLD_STEPS = (0, 1 << 10, 10 << 10, 100 << 10, 1 << 20, 10 << 20, 100 << 20, 1 << 30)
MIN_MIN_AUTO_INTERVAL_MINUTES = 0
MAX_MIN_AUTO_INTERVAL_MINUTES = 10080

DEFAULT_SCHEDULE_INTERVAL_MINUTES = 60
MIN_SCHEDULE_INTERVAL_MINUTES = 1
MAX_SCHEDULE_INTERVAL_MINUTES = 10080

TRIGGER_MASK_EVENT = 1
TRIGGER_MASK_SCHEDULE = 2
TRIGGER_MASK_STARTUP_CATCHUP = 4

DEFAULT_WATCHER_POLL_SECONDS = 15
MIN_WATCHER_POLL_SECONDS = 5
MAX_WATCHER_POLL_SECONDS = 300

DEFAULT_SCHEDULE_TICK_SECONDS = 30
MIN_SCHEDULE_TICK_SECONDS = 10
MAX_SCHEDULE_TICK_SECONDS = 300

DEFAULT_AUTOMATION_RUNTIME_TICK_SECONDS = 5
MIN_AUTOMATION_RUNTIME_TICK_SECONDS = 1
MAX_AUTOMATION_RUNTIME_TICK_SECONDS = 60

STARTUP_CATCHUP_GRACE_MINUTES = 5

SETTING_CLOSE_TO_TRAY = "runtime.close_to_tray"
SETTING_START_MINIMIZED = "runtime.start_minimized"

SETTING_AUTOSTART_ENABLED = "runtime.autostart_enabled"

SETTING_NOTIFICATIONS_ENABLED = "notifications.enabled"
SETTING_NOTIFICATIONS_SUCCESS_ENABLED = "notifications.success_enabled"
SETTING_NOTIFICATION_COOLDOWN_MINUTES = "notifications.cooldown_minutes"
SETTING_NOTIFICATION_LAST_SENT = "notifications.last_sent_by_key"

SETTING_UI_LANGUAGE = "ui.language"

DEFAULT_NOTIFICATIONS_ENABLED = True
DEFAULT_NOTIFICATIONS_SUCCESS_ENABLED = False
DEFAULT_NOTIFICATION_COOLDOWN_MINUTES = 30

MIN_NOTIFICATION_COOLDOWN_MINUTES = 1
MAX_NOTIFICATION_COOLDOWN_MINUTES = 1440

WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_AUTOSTART_VALUE_NAME = "AmviBackUp"

DEFAULT_CLOSE_TO_TRAY = True
DEFAULT_START_MINIMIZED = False

FREE_SPACE_MIN_RESERVE_BYTES = 1 * 1024 * 1024 * 1024
FREE_SPACE_RESERVE_RATIO = 0.10
HASH_CHUNK_SIZE = 4 * 1024 * 1024
COPY_CHUNK_SIZE = 4 * 1024 * 1024
SOURCE_RETRY_DELAYS = (2, 5, 10, 20, 40)
PLAN_DELETE_RETRY_DELAYS = (0.05, 0.15, 0.4)
SOURCE_LOCK_COOLDOWN_MS = 60_000

APP_QSS = """
QWidget {
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
    color: #edf2f8;
    background-color: #10141b;
}
QMainWindow, QDialog { background-color: #10141b; }
QFrame[role="card"] {
    background-color: #181e28;
    border: 1px solid #303b50;
    border-radius: 8px;
}
QFrame[role="surface"] { background-color: #181e28; border: none; }
QLabel[role="muted"] { color: #9aa8bc; }
QLabel[role="title"] { font-size: 16px; font-weight: 600; color: #edf2f8; }
QLabel[role="subtitle"] { font-size: 13px; font-weight: 600; color: #c9d9f4; }
QLabel[role="metric"] { font-size: 21px; font-weight: 700; color: #edf2f8; }
QLineEdit, QTextEdit, QSpinBox {
    background-color: #151c27;
    border: 1px solid #303b50;
    border-radius: 6px;
    padding: 6px 10px;
    color: #edf2f8;
}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus { border-color: #7c9cff; }
QPushButton {
    background-color: #2a3548;
    border: 1px solid #3a475e;
    border-radius: 6px;
    padding: 7px 12px;
    color: #edf2f8;
    font-weight: 500;
}
QPushButton:hover { background-color: #354157; border-color: #4a5873; }
QPushButton:disabled { background-color: #1a202c; border-color: #2d3748; color: #6b7c96; }
QPushButton[primary="true"] { background-color: #6d7cff; border: none; color: #ffffff; font-weight: 600; }
QPushButton[primary="true"]:hover { background-color: #7c9cff; }
QPushButton[role="danger"] { background-color: #3a1f24; border-color: #7a303a; color: #ef6974; }
QPushButton[role="ghost"] { background-color: transparent; border-color: transparent; }
QPushButton[role="ghost"]:hover { background-color: #2a3548; border-color: #3a475e; }
QTableWidget {
    background-color: #151c27;
    alternate-background-color: #1a2130;
    border: 1px solid #303b50;
    border-radius: 8px;
    gridline-color: #303b50;
    selection-background-color: #5368bb;
    selection-color: #ffffff;
}
QHeaderView::section {
    background-color: #181e28;
    border: none;
    border-bottom: 1px solid #303b50;
    padding: 8px 10px;
    color: #9aa8bc;
    font-weight: 600;
}
QMenu { background-color: #1a2130; border: 1px solid #3a475e; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 8px 24px; border-radius: 5px; color: #edf2f8; }
QMenu::item:selected { background-color: #2f4a8a; }
QMenu::separator { height: 1px; background: #303b50; margin: 6px 8px; }
QLabel, QCheckBox, QRadioButton { background: transparent; }
QWidget#helpRow { background: transparent; }
QToolTip { background-color: #1a2130; color: #edf2f8; border: 1px solid #3a475e; padding: 6px; }
QPushButton::menu-indicator { width: 0px; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #3a475e; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QProgressBar {
    background-color: #151c27;
    border: 1px solid #303b50;
    border-radius: 4px;
    color: #edf2f8;
    text-align: center;
}
QProgressBar::chunk { background-color: #7c9cff; border-radius: 3px; }
"""


class AppError(Exception):
    def __init__(self, code: str, user_message: str, technical_details: str = "") -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.technical_details = technical_details


class CancelledError(AppError):
    def __init__(self) -> None:
        super().__init__("OPERATION_CANCELLED", "The operation was cancelled by the user.")


class SourceKind(str, Enum):
    FILE = "file"
    FOLDER = "folder"


class StorageMode(str, Enum):
    DIRECT = "direct"
    ZIP = "zip"


class PlanState(str, Enum):
    ENABLED = "enabled"
    PAUSED = "paused"
    ARCHIVED = "archived"


class OperationType(str, Enum):
    BACKUP = "backup"
    VERIFY = "verify"
    TEST_RESTORE = "test_restore"
    RESTORE_BESIDE = "restore_beside"
    DELETE_SNAPSHOTS = "delete_snapshots"
    APPLY_RETENTION = "apply_retention"
    DELETE_PLAN_DATA = "delete_plan_data"
    ARCHIVE_PLAN = "archive_plan"
    RESTORE_TO_DIRECTORY = "restore_to_directory"
    RESTORE_SELECTED_FILES = "restore_selected_files"
    RESTORE_REPLACE_FILE = "restore_replace_file"
    RECOVER_PLAN = "recover_plan"
    CHECK_DATABASE = "check_database"
    LIST_SNAPSHOT_FILES = "list_snapshot_files"
    RUNTIME_TICK = "runtime_tick"
    REFRESH_HEALTH = "refresh_health"


class TriggerReason(str, Enum):
    MANUAL = "manual"
    MANUAL_FORCE = "manual_force"
    EVENT = "event"
    SCHEDULE = "schedule"
    STARTUP_CATCHUP = "startup_catchup"


class HealthState(str, Enum):
    PROTECTED = "protected"
    ATTENTION = "attention"
    ERROR = "error"
    PAUSED = "paused"
    UNPROTECTED = "unprotected"
    RUNNING = "running"
    ARCHIVED = "archived"


class IssueSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class NotificationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Language(str, Enum):
    ENGLISH = "en"
    RUSSIAN = "ru"
    CHINESE = "zh"
    HINDI = "hi"
    SPANISH = "es"
    FRENCH = "fr"
    ARABIC = "ar"


DEFAULT_UI_LANGUAGE = Language.ENGLISH.value

def _language_file_candidates() -> list[Path]:
    """Folders where the dictionary may live: next to the script, in a PyInstaller bundle, next to the exe."""
    folders = [Path(__file__).resolve().parent]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        folders.append(Path(bundle))
    if getattr(sys, "frozen", False):
        folders.append(Path(sys.executable).resolve().parent)
    result: list[Path] = []
    for folder in folders:
        candidate = folder / "amvibackup_languages.py"
        if candidate not in result:
            result.append(candidate)
    return result


def _load_language_data() -> Any:
    """Hooks up the mandatory shared interface dictionary amvibackup_languages.py."""
    candidates = _language_file_candidates()
    path = next((item for item in candidates if item.is_file()), candidates[0])
    if not path.is_file():
        raise RuntimeError(
            f"Interface dictionary not found: {path}\n"
            "Put amvibackup_languages.py in the same folder as the program."
        )
    import importlib.util
    spec = importlib.util.spec_from_file_location("amvibackup_languages", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for problem in module.validate():
        logging.warning("Language dictionary: %s", problem)
    return module


LANGUAGE_DATA = _load_language_data()
TRANSLATIONS: dict[str, dict[str, str]] = LANGUAGE_DATA.TRANSLATIONS


def available_language_options() -> list[tuple[str, str]]:
    """Pairs (name in its own language, code) for the language choice in settings."""
    return [(item.native_name, item.code) for item in LANGUAGE_DATA.LANGUAGES]


def apply_layout_direction(language: str) -> None:
    """The Arabic interface is mirrored right-to-left; the other languages run left-to-right."""
    QApplication.setLayoutDirection(
        Qt.LayoutDirection.RightToLeft if LANGUAGE_DATA.is_rtl(language) else Qt.LayoutDirection.LeftToRight
    )
    install_qt_translations(language)


# --- Localization helpers: every visible phrase comes from the dictionary -------------
_ACTIVE_UI_LANGUAGE = DEFAULT_UI_LANGUAGE
_QT_TRANSLATORS: list[Any] = []


def active_ui_language() -> str:
    """Language the user currently sees; used by helpers that have no I18nService at hand."""
    return _ACTIVE_UI_LANGUAGE


def set_active_ui_language(language: str) -> None:
    global _ACTIVE_UI_LANGUAGE
    if language in TRANSLATIONS:
        _ACTIVE_UI_LANGUAGE = language


def dictionary_text(key: str, language: str | None = None, **values: Any) -> str:
    """Dictionary text without an I18nService: falls back to English and never raises."""
    code = language if isinstance(language, str) and language in TRANSLATIONS else _ACTIVE_UI_LANGUAGE
    text = TRANSLATIONS.get(code, {}).get(key)
    if text is None:
        text = TRANSLATIONS[Language.ENGLISH.value].get(key, key)
    if not values:
        return text
    try:
        return text.format(**values)
    except (KeyError, ValueError, IndexError):
        return text


def system_ui_language() -> str:
    """UI language taken from the Windows language (used before the saved choice is known)."""
    try:
        from PySide6.QtCore import QLocale

        code = QLocale.system().name().split("_")[0].lower()
    except Exception:
        return DEFAULT_UI_LANGUAGE
    return code if code in TRANSLATIONS else DEFAULT_UI_LANGUAGE


def startup_language(controller: Any = None) -> str:
    """Language for start-up messages: the saved one if the database is open, else the system one."""
    try:
        value = controller.i18n.language if controller is not None else None
        if isinstance(value, str) and value in TRANSLATIONS:
            return value
    except Exception:
        pass
    return system_ui_language()


def startup_error_text(error: Any, language: str) -> str:
    """Start-up error text from the dictionary by error code (never the raw internal message)."""
    key = f"error.{getattr(error, 'code', '')}"
    if key in TRANSLATIONS.get(language, {}):
        return TRANSLATIONS[language][key]
    return dictionary_text("error.generic", language)


PROGRESS_STAGE_KEYS = {
    "Publishing version…": "progress.stage.publishing",
    "Restoring selected files…": "progress.stage.restoring_selected",
    "Checking the file before replacing…": "progress.stage.checking_before_replace",
    "Preparing the file list…": "progress.stage.preparing_list",
    "Cleaning up old versions…": "progress.stage.cleaning_old",
    "Deleting the plan and its data…": "progress.stage.deleting_plan",
    "Deleting versions…": "progress.stage.deleting_versions",
    "Verifying the ZIP version…": "progress.stage.verifying_zip",
    "Waiting for a busy file…": "progress.stage.waiting_busy_file",
    "Verifying the version…": "progress.stage.verifying",
    "Verifying the version after a failure…": "progress.stage.verifying_after_failure",
    "Creating the version…": "progress.stage.creating",
    "Creating the ZIP version…": "progress.stage.creating_zip",
}
_PROGRESS_VERSION_RE = re.compile(r"^Version #(\d+)$")


def localized_progress_title(title: str, language: str | None = None) -> str:
    """Services report progress with fixed internal titles; the screen shows the dictionary text."""
    key = PROGRESS_STAGE_KEYS.get(title)
    return dictionary_text(key, language) if key else title


def localized_progress_path(path: str, language: str | None = None) -> str:
    match = _PROGRESS_VERSION_RE.match(path)
    if match:
        return dictionary_text("progress.version_number", language, n=match.group(1))
    return path


def install_qt_translations(language: str) -> bool:
    """Best effort: Qt's own texts (edit menus, file dialogs) in the dictionary language."""
    try:
        from PySide6.QtCore import QCoreApplication, QLibraryInfo, QTranslator
    except ImportError:
        return False
    application = QCoreApplication.instance()
    if application is None:
        return False
    for old in _QT_TRANSLATORS:
        application.removeTranslator(old)
    _QT_TRANSLATORS.clear()
    if language == Language.ENGLISH.value:
        return True
    code = {"zh": "zh_CN"}.get(language, language)
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translator = QTranslator(application)
    for name in (f"qt_{code}", f"qtbase_{code}"):
        if translator.load(name, folder):
            application.installTranslator(translator)
            _QT_TRANSLATORS.append(translator)
            return True
    return False


_StockMessageBox = QMessageBox


def _button_label_keys() -> dict:
    standard = _StockMessageBox.StandardButton
    return {
        standard.Yes: "common.yes",
        standard.No: "common.no",
        standard.Ok: "common.ok",
        standard.Cancel: "common.cancel",
        standard.Close: "common.close",
        standard.Save: "common.save",
    }


def build_localized_message_box(icon: Any, parent: Any, title: str, text: str, buttons: Any, default_button: Any) -> Any:
    """A message box whose standard buttons use dictionary texts instead of Qt's English ones."""
    standard = _StockMessageBox.StandardButton
    box = _StockMessageBox(icon, title, text, buttons, parent)
    for member, key in _button_label_keys().items():
        button = box.button(member)
        if button is not None:
            button.setText(dictionary_text(key))
    if default_button != standard.NoButton:
        box.setDefaultButton(default_button)
    return box


class QMessageBox(_StockMessageBox):
    """QMessageBox with Yes/No/OK/Cancel buttons shown in the language of the interface."""

    @staticmethod
    def _run(icon: Any, parent: Any, title: str, text: str, buttons: Any, default_button: Any) -> Any:
        box = build_localized_message_box(icon, parent, title, text, buttons, default_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None:
            return _StockMessageBox.StandardButton.NoButton
        return box.standardButton(clicked)

    @staticmethod
    def information(parent, title, text, buttons=_StockMessageBox.StandardButton.Ok,
                    defaultButton=_StockMessageBox.StandardButton.NoButton):
        return QMessageBox._run(_StockMessageBox.Icon.Information, parent, title, text, buttons, defaultButton)

    @staticmethod
    def warning(parent, title, text, buttons=_StockMessageBox.StandardButton.Ok,
                defaultButton=_StockMessageBox.StandardButton.NoButton):
        return QMessageBox._run(_StockMessageBox.Icon.Warning, parent, title, text, buttons, defaultButton)

    @staticmethod
    def critical(parent, title, text, buttons=_StockMessageBox.StandardButton.Ok,
                 defaultButton=_StockMessageBox.StandardButton.NoButton):
        return QMessageBox._run(_StockMessageBox.Icon.Critical, parent, title, text, buttons, defaultButton)

    @staticmethod
    def question(parent, title, text,
                 buttons=_StockMessageBox.StandardButton.Yes | _StockMessageBox.StandardButton.No,
                 defaultButton=_StockMessageBox.StandardButton.NoButton):
        return QMessageBox._run(_StockMessageBox.Icon.Question, parent, title, text, buttons, defaultButton)


@dataclass(frozen=True)
class SourceDescriptor:
    path: Path
    kind: SourceKind


def assert_scanned_source_handle(stream: Any, entry: dict[str, Any]) -> None:
    try:
        info = os.fstat(stream.fileno())
    except OSError as exc:
        raise AppError("SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                       "Could not check the opened source.", str(exc)) from exc
    if (info.st_dev != entry.get("scan_device", info.st_dev)
            or info.st_ino != entry.get("scan_file_id", info.st_ino)):
        raise AppError("SOURCE_IDENTITY_MISMATCH", "A different file was opened instead of the scanned source.")
    if info.st_size != int(entry["size_bytes"]) or info.st_mtime_ns != int(entry["mtime_ns"]):
        raise AppError("SOURCE_CHANGED_DURING_READ", "The source changed after scanning.")


def retryable_source_read(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in (32, 33)
    return exc.errno in (errno.EACCES, errno.EBUSY, errno.ETXTBSY)


def confirmed_source_identity(path: Path, kind: str) -> str:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise AppError("SOURCE_IDENTITY_UNAVAILABLE", "The source is a redirect (link or junction).")
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise AppError("SOURCE_NOT_FOUND", "Source not found.", str(exc)) from exc
    except PermissionError as exc:
        locked = retryable_source_read(exc)
        raise AppError("SOURCE_LOCKED" if locked else "SOURCE_ACCESS_DENIED",
                       "The source is temporarily busy." if locked else "No access to the source.", str(exc)) from exc
    if not info.st_dev or not info.st_ino:
        raise AppError("SOURCE_IDENTITY_UNAVAILABLE", "The file system does not provide a file identifier.")
    if not ((kind == SourceKind.FILE.value and path.is_file()) or
            (kind == SourceKind.FOLDER.value and path.is_dir())):
        raise AppError("SOURCE_KIND_MISMATCH", "The source type changed.")
    created = getattr(info, "st_birthtime_ns", None)
    if created is None and os.name == "nt":
        created = info.st_ctime_ns
    return json.dumps({"volume": info.st_dev, "file_id": info.st_ino,
                       "created_ns": created, "kind": kind}, sort_keys=True)


_TEMP_SIBLING_SUFFIXES = (".tmp", ".temp", ".bak", ".wbk", ".xlk", ".asd")
_REPLACED_FILE_SCAN_LIMIT = 5000


def file_replaced_in_place(path: Path, recorded_identity: str) -> bool:
    """True when the file now at ``path`` is a different object than the recorded one and the recorded object
    no longer exists in the same folder. That is what an atomic save of Word, LibreOffice and similar editors
    looks like (new file written, old one removed). If the old object still exists under another name,
    it was renamed or substituted, and the user has to confirm it."""
    try:
        recorded = json.loads(recorded_identity)
        if recorded.get("kind") != SourceKind.FILE.value:
            return False
        volume, file_id = recorded["volume"], recorded["file_id"]
        if path.is_symlink() or not path.is_file():
            return False
        own_name = os.path.normcase(path.name)
        checked = 0
        with os.scandir(path.parent) as entries:
            for entry in entries:
                name = entry.name
                if os.path.normcase(name) == own_name:
                    continue
                lowered = name.lower()
                if lowered.startswith("~") or lowered.endswith(_TEMP_SIBLING_SUFFIXES):
                    continue
                checked += 1
                if checked > _REPLACED_FILE_SCAN_LIMIT:
                    return False
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = os.stat(entry.path)
                except OSError:
                    continue
                if info.st_dev == volume and info.st_ino == file_id:
                    return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


@dataclass(frozen=True)
class PlanDeletionPreview:
    plan_id: str
    display_name: str
    source_path: str
    storage_path: str
    snapshot_count: int
    stored_size_bytes: int
    pinned_count: int
    token: str
    resuming: bool = False
    warnings: list[str] = field(default_factory=list)
    has_problems: bool = False
@dataclass(frozen=True)
class PlanSummary:
    id: str
    display_name: str
    source_path: str
    state: str
    storage_path: str
    keep_last_n: int
    snapshot_count: int
    last_snapshot_at_utc_ms: int | None
    last_operation_state: str | None
    on_change_enabled: bool
    on_schedule_enabled: bool
    pending_trigger_mask: int
    last_auto_backup_at_utc_ms: int | None


@dataclass(frozen=True)
class PlanDetails:
    id: str
    display_name: str
    note: str
    source_path: str
    source_kind: str
    storage_mode: str
    state: str
    storage_path: str
    storage_root_id: str
    keep_last_n: int
    last_success_snapshot_id: str | None
    snapshot_count: int
    last_snapshot_at_utc_ms: int | None
    last_operation_state: str | None
    on_change_enabled: bool
    change_debounce_seconds: int
    min_auto_interval_minutes: int
    on_schedule_enabled: bool
    schedule_interval_minutes: int
    quiet_hours_enabled: bool
    quiet_hours_start_local: str | None
    quiet_hours_end_local: str | None
    manual_allowed_quiet_hours: bool
    last_auto_backup_at_utc_ms: int | None
    pending_trigger_mask: int
    pending_trigger_since_utc_ms: int | None
    last_source_scan_fingerprint: str
    source_identity_json: str | None = None
    retention_mode: str = RETENTION_MODE_COUNT
    retention_daily: int = 0
    retention_weekly: int = 0
    retention_monthly: int = 0
    change_threshold_bytes: int = 0


@dataclass(frozen=True)
class RetentionPolicy:
    mode: str = RETENTION_MODE_COUNT
    keep_last_n: int = DEFAULT_KEEP_LAST_N
    daily: int = 0
    weekly: int = 0
    monthly: int = 0


@dataclass(frozen=True)
class RetentionPreview:
    plan_id: str
    token: str
    delete_snapshot_ids: tuple[str, ...]
    delete_sequence_numbers: tuple[int, ...]
    protected_snapshot_ids: tuple[str, ...]
    clock_blocked: bool = False

    @property
    def delete_count(self) -> int:
        return len(self.delete_snapshot_ids)

@dataclass(frozen=True)
class TriggerPolicy:
    on_change_enabled: bool
    change_debounce_seconds: int
    min_auto_interval_minutes: int
    on_schedule_enabled: bool
    schedule_interval_minutes: int
    quiet_hours_enabled: bool
    quiet_hours_start_local: str | None
    quiet_hours_end_local: str | None
    manual_allowed_quiet_hours: bool
    change_threshold_bytes: int = 0


@dataclass(frozen=True)
class AutoDecision:
    plan_id: str
    action: str
    reason: str
    detail: str
    trigger_mask: int = 0


@dataclass(frozen=True)
class SnapshotInfo:
    id: str
    plan_id: str
    operation_id: str
    sequence_no: int
    state: str
    storage_mode: str
    created_at_utc_ms: int
    artifact_relpath: str
    manifest_relpath: str
    logical_size_bytes: int
    stored_size_bytes: int
    file_count: int
    source_fingerprint: str
    manifest_sha256: str
    verification_state: str
    verified_at_utc_ms: int | None
    pinned: bool
    display_name: str = ""
    source_path: str = ""
    source_kind: str = ""
    plan_state: str = ""
    storage_path: str = ""


@dataclass
class ProgressEvent:
    operation_id: str
    title: str
    current_path: str = ""
    completed_files: int = 0
    total_files: int = 0
    completed_bytes: int = 0
    total_bytes: int = 0
    cancellable: bool = True


@dataclass
class OperationResult:
    operation_id: str
    success: bool
    message: str
    error_code: str | None = None
    trigger_reason: str | None = None
    plan_id: str | None = None
    result_code: str | None = None
    destination_path: str | None = None
    result_data: dict[str, Any] | None = None


@dataclass(frozen=True)
class SourceProbe:
    available: bool
    readable: bool
    kind_matches: bool
    error_code: str | None
    detail: str


@dataclass(frozen=True)
class StorageProbe:
    available: bool
    writable: bool
    free_bytes: int | None
    error_code: str | None
    detail: str


@dataclass(frozen=True)
class IssueDraft:
    dedupe_key: str
    plan_id: str | None
    severity: str
    code: str
    title: str
    detail: str
    action_label: str | None = None


@dataclass(frozen=True)
class HealthIssue:
    key: str
    plan_id: str | None
    severity: str
    code: str
    title: str
    detail: str
    created_at_utc_ms: int
    last_seen_at_utc_ms: int
    action_label: str | None = None


@dataclass(frozen=True)
class PlanHealth:
    plan_id: str
    state: str
    title: str
    detail: str
    source_available: bool | None
    storage_available: bool | None
    committed_snapshot_count: int
    last_snapshot_at_utc_ms: int | None
    last_operation_state: str | None
    active_issue_count: int


@dataclass(frozen=True)
class SnapshotArtifactPaths:
    snapshot_root: Path
    direct_data_root: Path
    zip_artifact: Path
    manifest_path: Path
    snapshot_metadata_path: Path

@dataclass
class AppPaths:
    root: Path
    db_path: Path
    logs_dir: Path
    log_path: Path

    @classmethod
    def create(cls) -> "AppPaths":
        local = os.environ.get("LOCALAPPDATA")
        root = Path(local) / APP_NAME if local else Path.home() / "AppData" / "Local" / APP_NAME
        logs = root / "logs"
        for directory in (root, logs, root / "recovery"):
            directory.mkdir(parents=True, exist_ok=True)
        return cls(root, root / "amvibackup.sqlite3", logs, logs / "amvibackup.log")


class InstallerMutex:
    """Named Windows mutex. The installer (Inno Setup AppMutex) uses it to see that the app is running."""

    def __init__(self, name: str = INSTALLER_MUTEX_NAME, kernel32: Any = None, platform_name: str | None = None) -> None:
        self.name = name
        self._handle: Any = None
        self._kernel32 = kernel32
        if kernel32 is None and (platform_name or platform.system()) == "Windows":
            try:
                import ctypes
                from ctypes import wintypes
                self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
                self._kernel32.CreateMutexW.restype = ctypes.c_void_p
                self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                self._kernel32.CloseHandle.restype = wintypes.BOOL
            except (AttributeError, OSError):
                self._kernel32 = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        if self._kernel32 is None or self._handle is not None:
            return self.held
        try:
            handle = self._kernel32.CreateMutexW(None, False, self.name)
        except OSError:
            logging.warning("Installer mutex is unavailable", exc_info=True)
            return False
        self._handle = handle or None
        return self.held

    def release(self) -> None:
        if self._handle is not None and self._kernel32 is not None:
            try:
                self._kernel32.CloseHandle(self._handle)
            except OSError:
                logging.debug("Unable to close installer mutex", exc_info=True)
        self._handle = None


def apply_installer_language(paths: AppPaths, repo: Any, i18n: Any) -> str | None:
    """First run only: take the language chosen in the installer; later runs keep the user's own choice."""
    marker = paths.root / INSTALLER_LANGUAGE_FILE
    if not marker.is_file():
        return None
    applied: str | None = None
    try:
        code = marker.read_text(encoding="utf-8-sig").strip().lower()
        if code in {item.value for item in Language} and repo.get_setting(SETTING_UI_LANGUAGE, None) is None:
            i18n.set_language(code)
            applied = code
    except (OSError, UnicodeError, AppError):
        logging.warning("Installer language marker was not applied", exc_info=True)
    finally:
        try:
            marker.unlink()
        except OSError:
            logging.debug("Unable to remove installer language marker", exc_info=True)
    return applied


def register_support_first_launch(paths: AppPaths, repo: Any) -> bool:
    """Consume the first-install marker, not the language marker. Keep it if storing the request failed."""
    marker = paths.root / SUPPORT_FIRST_RUN_FILE
    if not marker.is_file():
        return False
    try:
        shown = repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_SHOWN, False)
        if not shown:
            repo.set_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, True)
        marker.unlink()
        return not shown
    except (OSError, AppError):
        logging.warning("Unable to register first-install Support request", exc_info=True)
        return False


UNINSTALLER_NAME_PATTERN = re.compile(r"^unins\d{3}\.exe$", re.IGNORECASE)


def installed_app_dir() -> Path | None:
    """Folder of the installed program, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def find_uninstaller(app_dir: Path | None = None) -> Path | None:
    """Path of the Inno Setup uninstaller (unins000.exe) next to the program, if any."""
    folder = Path(app_dir) if app_dir is not None else installed_app_dir()
    if folder is None or not folder.is_dir():
        return None
    try:
        candidates = sorted(folder.iterdir())
    except OSError:
        return None
    for candidate in candidates:
        if candidate.is_file() and UNINSTALLER_NAME_PATTERN.match(candidate.name):
            return candidate.resolve()
    return None


def is_trusted_uninstaller(path: Path, app_dir: Path | None) -> bool:
    """Only unins###.exe stored directly in the program folder may be started."""
    if app_dir is None:
        return False
    try:
        resolved = Path(path).resolve()
        return (
            UNINSTALLER_NAME_PATTERN.match(resolved.name) is not None
            and resolved.is_file()
            and resolved.parent == Path(app_dir).resolve()
        )
    except OSError:
        return False


_PENDING_UNINSTALLER: list[Path] = []


def launch_uninstaller_helper(uninstaller: Path) -> None:
    """Remembers the uninstaller; this same program starts it once it has closed (see start_pending_uninstaller)."""
    if not is_trusted_uninstaller(Path(uninstaller), installed_app_dir()):
        raise OSError("The uninstaller path is not trusted")
    _PENDING_UNINSTALLER.clear()
    _PENDING_UNINSTALLER.append(Path(uninstaller).resolve())


def start_pending_uninstaller(launcher: Callable[[str], Any] | None = None) -> bool:
    """Starts the remembered uninstaller (if any) after the program released its locks."""
    if not _PENDING_UNINSTALLER:
        return False
    target = _PENDING_UNINSTALLER.pop()
    try:
        if launcher is not None:
            launcher(str(target))
        elif hasattr(os, "startfile"):
            os.startfile(str(target))
        else:
            return False
    except OSError:
        logging.exception("Unable to start the uninstaller")
        return False
    return True


APP_USER_MODEL_ID = "AmviBackUp.App"
SINGLE_INSTANCE_COMMAND = b"show"
SINGLE_INSTANCE_ACK = b"ok\n"
_LAST_INSTANCE_VERSION: list[str] = [""]


def instance_ack() -> bytes:
    """Answer of the running copy to a second launch: 'ok' plus its own version."""
    return b"ok " + APP_VERSION.encode("utf-8") + b"\n"


def parse_instance_reply(data: bytes) -> tuple[bool, str]:
    text = bytes(data).decode("utf-8", "replace").strip()
    if text == "ok":
        return True, ""
    if text.startswith("ok "):
        return True, text[3:].strip()
    return False, ""


def last_existing_instance_version() -> str:
    return _LAST_INSTANCE_VERSION[0]


def other_version_message(running_version: str) -> str:
    shown = running_version or "?"
    return (
        f"{APP_NAME} {shown} is already running and was brought to the front. "
        f"It is a different version from the one you just started ({APP_VERSION}). "
        "Close it first (tray icon -> Exit) and start again.\n\n"
        f"{APP_NAME} {shown} уже запущен и показан на экране. "
        f"Это другая версия, чем та, которую вы только что запустили ({APP_VERSION}). "
        "Закройте её (значок в трее -> Выход) и запустите снова."
    )
APP_ICON_BACKGROUND = "#F58220"


def single_instance_name(db_path: Path) -> str:
    """Name of the local socket of the running copy; one name per data folder (so per user)."""
    text = str(Path(db_path).expanduser().resolve(strict=False)).lower()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return f"{APP_NAME}-activate-{digest}"


def set_app_user_model_id(
    value: str = APP_USER_MODEL_ID,
    shell32: Any = None,
    platform_name: str | None = None,
) -> bool:
    """Gives the process the same taskbar identity as the installer shortcuts."""
    if (platform_name or platform.system()) != "Windows":
        return False
    try:
        if shell32 is None:
            import ctypes

            shell32 = ctypes.windll.shell32
        return shell32.SetCurrentProcessExplicitAppUserModelID(value) == 0
    except (AttributeError, OSError):
        return False


def allow_foreground_for_existing_instance() -> None:
    """Lets the already running copy bring its window to the front (Windows focus rules)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    except (AttributeError, OSError):
        logging.debug("AllowSetForegroundWindow is unavailable", exc_info=True)


def request_existing_instance(name: str, timeout_ms: int = 1500) -> bool:
    """Asks an already running copy to show its window. True only if that copy answered."""
    try:
        from PySide6.QtNetwork import QLocalSocket
    except ImportError:
        return False
    allow_foreground_for_existing_instance()
    socket = QLocalSocket()
    try:
        socket.connectToServer(name)
        if not socket.waitForConnected(timeout_ms):
            return False
        socket.write(SINGLE_INSTANCE_COMMAND + b" " + APP_VERSION.encode("utf-8") + b"\n")
        socket.flush()
        if not socket.waitForBytesWritten(timeout_ms):
            return False
        if not socket.waitForReadyRead(timeout_ms):
            return False
        acknowledged, version = parse_instance_reply(bytes(socket.readAll().data()))
        if acknowledged:
            _LAST_INSTANCE_VERSION[0] = version
        return acknowledged
    finally:
        socket.abort()


def request_existing_instance_with_retries(
    name: str,
    attempts: int = 6,
    delay_seconds: float = 0.5,
    request: Callable[[str], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> bool:
    """The first copy may still be starting: try a few times before giving up."""
    ask = request or (lambda value: request_existing_instance(value, 500))
    pause = sleep or time.sleep
    for attempt in range(max(1, attempts)):
        if ask(name):
            return True
        if attempt + 1 < attempts:
            pause(delay_seconds)
    return False


def build_app_icon() -> QIcon:
    """The application icon (white AB on orange), drawn in code so it needs no extra files."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QFont, QPainter, QPixmap

    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(APP_ICON_BACKGROUND))
            radius = size * 0.2
            painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
            font = QFont("Segoe UI")
            font.setBold(True)
            font.setPixelSize(max(6, int(size * 0.5)))
            painter.setFont(font)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(
                QRectF(0, 0, size, size),
                int(Qt.AlignmentFlag.AlignCenter),
                "AB",
            )
        finally:
            painter.end()
        icon.addPixmap(pixmap)
    return icon


class SingleInstanceServer(QObject):
    """Listens for 'show the window' requests from a second launch of the program."""

    activation_requested = Signal()

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtNetwork import QLocalServer

        self.name = name
        self.listening = False
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._on_connection)

    def start(self) -> bool:
        from PySide6.QtNetwork import QLocalServer

        QLocalServer.removeServer(self.name)
        self.listening = bool(self.server.listen(self.name))
        if not self.listening:
            logging.warning(
                "Single-instance socket is unavailable: %s",
                self.server.errorString(),
            )
        return self.listening

    def stop(self) -> None:
        self.server.close()
        self.listening = False

    def _on_connection(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                break
            socket.disconnected.connect(socket.deleteLater)
            socket.readyRead.connect(lambda s=socket: self._handle(s))
            if socket.bytesAvailable():
                self._handle(socket)

    def _handle(self, socket: Any) -> None:
        data = bytes(socket.readAll().data())
        if SINGLE_INSTANCE_COMMAND not in data:
            return
        socket.write(instance_ack())
        socket.flush()
        socket.disconnectFromServer()
        self.activation_requested.emit()


class ApplicationInstanceGuard:
    def __init__(self, db_path: Path) -> None:
        canonical_db = db_path.expanduser().resolve(strict=False)
        self.lock_path = canonical_db.with_name(
            canonical_db.name + ".instance.lock"
        )
        self._lock = QLockFile(str(self.lock_path))
        self._lock.setStaleLockTime(0)
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> None:
        if self._held:
            return

        if self._lock.tryLock(0):
            self._held = True
            return

        error = self._lock.error()

        if error == QLockFile.LockError.LockFailedError:
            raise AppError(
                "APP_ALREADY_RUNNING",
                "AmviBackUp is already using this database. "
                "Open the existing window from the tray icon.",
                str(self.lock_path),
            )

        if error == QLockFile.LockError.PermissionError:
            raise AppError(
                "APP_LOCK_ACCESS_DENIED",
                "No access to the AmviBackUp data folder. "
                "Could not protect the working database.",
                str(self.lock_path),
            )

        raise AppError(
            "APP_LOCK_FAILED",
            "Could not set up the single-instance protection.",
            f"{self.lock_path}: {error}",
        )

    def release(self) -> None:
        if self._held:
            self._lock.unlock()
            self._held = False


class I18nService:
    def __init__(
        self,
        repo: "Repository",
        default_language: str = DEFAULT_UI_LANGUAGE,
    ) -> None:
        self.repo = repo
        self.default_language = self._validate_language(
            default_language
        )

    @staticmethod
    def _validate_language(language: str) -> str:
        if language not in {item.value for item in Language}:
            raise AppError(
                "UI_LANGUAGE_INVALID",
                f"Unsupported UI language: {language}",
            )
        return language

    @property
    def language(self) -> str:
        value = self.repo.get_setting(
            SETTING_UI_LANGUAGE,
            self.default_language,
        )
        if not isinstance(value, str):
            raise AppError(
                "UI_LANGUAGE_INVALID",
                "Saved UI language must be a string.",
            )
        validated = self._validate_language(value)
        set_active_ui_language(validated)
        return validated

    def set_language(self, language: str) -> None:
        normalized = self._validate_language(language)
        self.repo.set_setting(SETTING_UI_LANGUAGE, normalized)
        set_active_ui_language(normalized)

    def tr(self, key: str, **values: Any) -> str:
        language = self.language
        text = TRANSLATIONS.get(language, {}).get(key)

        if text is None:
            text = TRANSLATIONS[Language.ENGLISH.value].get(key)

        if text is None:
            raise AppError(
                "TRANSLATION_KEY_MISSING",
                f"Missing translation key: {key}",
            )

        try:
            return text.format(**values)
        except (KeyError, ValueError, IndexError) as exc:
            raise AppError(
                "TRANSLATION_FORMAT_INVALID",
                f"Cannot format translation: {key}",
                str(exc),
            ) from exc

    def has_key(self, key: str) -> bool:
        return key in TRANSLATIONS[Language.ENGLISH.value]


    def health_detail(self, state: str) -> str:
        key = f"health.detail.{state}"
        return self.tr(key) if self.has_key(key) else self.tr(
            "health.detail.error"
        )


    def issue_title(self, code: str) -> str:
        key = f"issue.{code}"
        return self.tr(key) if self.has_key(key) else self.tr(
            "issue.unknown"
        )


    def issue_detail(self, code: str) -> str:
        key = f"issue.detail.{code}"
        return self.tr(key) if self.has_key(key) else self.tr(
            "issue.detail.generic"
        )


    def operation_message(
        self,
        result: OperationResult,
    ) -> str:
        if result.error_code:
            if result.error_code == "OPERATION_CANCELLED":
                return self.tr("operation.cancelled")

            key = f"operation.error.{result.error_code}"
            if self.has_key(key):
                return self.tr(key)
            key = f"error.{result.error_code}"
            if self.has_key(key):
                return self.tr(key)
            return self.tr("operation.failed")

        if not result.success:
            return self.tr("operation.failed")

        if result.result_code == "PLAN_DELETE_SUCCEEDED":
            data = result.result_data or {}
            if data.get("failed_files"):
                return self.tr(
                    "plan_delete.leftovers_body",
                    deleted=data.get("deleted_files", 0),
                    failed=data.get("failed_files", 0),
                )
            return self.tr("plan_delete.succeeded")
        if result.result_code == "RESTORE_TEST_SUCCEEDED":
            return self.tr("recovery.test_ok")
        success_keys = {
            "BACKUP_CREATED": "operation.backup_created",
            "BACKUP_NO_CHANGES": "operation.no_changes",
            "VERIFY_SUCCEEDED": "operation.verify_succeeded",
            "RESTORE_SUCCEEDED": "operation.restore_succeeded",
            "RESTORE_SELECTED_SUCCEEDED": "operation.restore_selected_succeeded",
            "DELETE_SUCCEEDED": "operation.delete_succeeded",
            "RECOVERY_FINISHED": "operation.recovery_finished",
        }
        if result.result_code == "RESTORE_TO_DIRECTORY_SUCCEEDED":
            return self.tr(
                "operation.restore_to_directory_succeeded",
                path=result.destination_path or "",
            )
        if result.result_code == "RESTORE_SELECTED_SUCCEEDED":
            return self.tr(
                "operation.restore_selected_succeeded",
                path=result.destination_path or "",
            )
        if result.result_code == "RESTORE_REPLACE_SUCCEEDED":
            return self.tr(
                "operation.replace_file_succeeded",
                path=result.destination_path or "",
            )
        if result.result_code in success_keys:
            return self.tr(success_keys[result.result_code])

        # Old operations without a result_code stay compatible.
        # We do not try to guess the outcome of a backup from the message text.
        try:
            operation = self.repo.operation(result.operation_id)
            operation_type = operation["type"]
        except AppError:
            return self.tr("operation.failed")

        if operation_type == OperationType.VERIFY.value:
            return self.tr("operation.verify_succeeded")
        if operation_type == OperationType.RESTORE_BESIDE.value:
            return self.tr("operation.restore_succeeded")
        if operation_type == OperationType.DELETE_SNAPSHOTS.value:
            return self.tr("operation.delete_succeeded")

        return self.tr("operation.failed")

    def error_message(self, error: AppError) -> str:
        key = f"error.{error.code}"
        if self.has_key(key):
            return self.tr(key)
        return self.tr("error.generic")


def utc_ms() -> int:
    return int(time.time() * 1000)


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fmt_time(value: int | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value / 1000).strftime("%d.%m.%Y %H:%M")


def fmt_size(value: int) -> str:
    size = float(max(0, value))
    units = [dictionary_text(f"units.{name}") for name in ("b", "kb", "mb", "gb", "tb")]
    for index, unit in enumerate(units):
        if size < 1024 or index == len(units) - 1:
            return f"{int(size)} {unit}" if index == 0 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {units[-1]}"


def validate_local_time_text(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value.strip()

    if not normalized or normalized.count(":") != 1:
        raise AppError(
            "TRIGGER_POLICY_INVALID",
            "Quiet hours time must use the format H:MM or HH:MM.",
        )

    hour_text, minute_text = normalized.split(":", 1)

    if (
        not hour_text.isdigit()
        or not minute_text.isdigit()
        or len(hour_text) not in {1, 2}
        or len(minute_text) not in {1, 2}
    ):
        raise AppError(
            "TRIGGER_POLICY_INVALID",
            "Quiet hours time must use the format H:MM or HH:MM.",
        )

    hour = int(hour_text)
    minute = int(minute_text)

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise AppError(
            "TRIGGER_POLICY_INVALID",
            "Quiet hours time must be in the range 00:00–23:59.",
        )

    return f"{hour:02d}:{minute:02d}"


def trigger_mask_for_reason(reason: TriggerReason) -> int:
    if reason == TriggerReason.EVENT:
        return TRIGGER_MASK_EVENT
    if reason == TriggerReason.SCHEDULE:
        return TRIGGER_MASK_SCHEDULE
    if reason == TriggerReason.STARTUP_CATCHUP:
        return TRIGGER_MASK_STARTUP_CATCHUP
    return 0


def has_trigger_mask(mask: int, required_mask: int) -> bool:
    return bool(int(mask) & int(required_mask))


def trigger_reason_label(reason: TriggerReason) -> str:
    labels = {
        TriggerReason.MANUAL: "manual",
        TriggerReason.MANUAL_FORCE: "manual_force",
        TriggerReason.EVENT: "event",
        TriggerReason.SCHEDULE: "schedule",
        TriggerReason.STARTUP_CATCHUP: "startup_catchup",
    }
    return labels[reason]


def local_minutes_from_text(value: str) -> int:
    canonical = validate_local_time_text(value)
    if canonical is None:
        raise AppError(
            "TRIGGER_POLICY_INVALID",
            "Quiet hours time is not set.",
        )

    hour_text, minute_text = canonical.split(":", 1)
    return int(hour_text) * 60 + int(minute_text)


def is_in_quiet_hours(
    now_local: datetime,
    start_local: str | None,
    end_local: str | None,
) -> bool:
    if start_local is None or end_local is None:
        return False

    start_minutes = local_minutes_from_text(start_local)
    end_minutes = local_minutes_from_text(end_local)
    now_minutes = now_local.hour * 60 + now_local.minute

    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes

    return now_minutes >= start_minutes or now_minutes < end_minutes


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, cancelled: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if cancelled and cancelled():
                raise CancelledError()
            chunk = stream.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fsync_file(path: Path) -> None:
    """Best effort: force a freshly written file to disk before the version is published as committed."""
    try:
        with open(path, "r+b") as stream:
            os.fsync(stream.fileno())
    except (OSError, ValueError):
        logging.debug("fsync skipped for %s", path, exc_info=True)


def source_metadata_fingerprint(
    source: SourceDescriptor,
) -> str:
    return source_metadata_scan(source)[0]


def source_metadata_scan(
    source: SourceDescriptor,
) -> tuple[str, int]:
    """Returns a deterministic metadata fingerprint without reading file content.

    Used only by the polling watcher. It does not create a manifest,
    does not copy data and does not start the backup pipeline.
    """
    entries: list[dict[str, Any]] = []

    try:
        if source.kind == SourceKind.FILE:
            stat_result = source.path.stat()
            entries.append(
                {
                    "relative_path": source.path.name,
                    "entry_type": "file",
                    "size_bytes": stat_result.st_size,
                    "mtime_ns": stat_result.st_mtime_ns,
                }
            )
        else:
            for directory, directories, files in os.walk(
                source.path,
                followlinks=False,
            ):
                root = Path(directory)

                directories[:] = sorted(
                    [
                        name
                        for name in directories
                        if not (root / name).is_symlink()
                    ],
                    key=str.casefold,
                )

                for name in directories:
                    path = root / name
                    try:
                        stat_result = path.stat()
                    except (FileNotFoundError, PermissionError):
                        continue

                    entries.append(
                        {
                            "relative_path": path.relative_to(
                                source.path
                            ).as_posix(),
                            "entry_type": "directory",
                            "size_bytes": 0,
                            "mtime_ns": stat_result.st_mtime_ns,
                        }
                    )

                for name in sorted(files, key=str.casefold):
                    path = root / name

                    if path.is_symlink():
                        continue

                    try:
                        stat_result = path.stat()
                    except FileNotFoundError as exc:
                        raise AppError(
                            "SOURCE_CHANGED_DURING_SCAN",
                            "The source changed during the polling scan.",
                            str(exc),
                        ) from exc
                    except PermissionError as exc:
                        raise AppError(
                            "SOURCE_ACCESS_DENIED",
                            "No access to the source file.",
                            str(exc),
                        ) from exc

                    entries.append(
                        {
                            "relative_path": path.relative_to(
                                source.path
                            ).as_posix(),
                            "entry_type": "file",
                            "size_bytes": stat_result.st_size,
                            "mtime_ns": stat_result.st_mtime_ns,
                        }
                    )
    except FileNotFoundError as exc:
        raise AppError(
            "SOURCE_NOT_FOUND",
            "Source not found during the polling scan.",
            str(exc),
        ) from exc
    except PermissionError as exc:
        raise AppError(
            "SOURCE_ACCESS_DENIED",
            "No access to the source.",
            str(exc),
        ) from exc
    except OSError as exc:
        raise AppError(
            "SOURCE_SCAN_FAILED",
            "Could not scan the source metadata.",
            str(exc),
        ) from exc

    entries.sort(
        key=lambda item: (
            item["relative_path"].casefold(),
            item["entry_type"],
        )
    )

    material = {
        "source_kind": source.kind.value,
        "entries": entries,
    }

    total_bytes = sum(
        int(item["size_bytes"]) for item in entries if item["entry_type"] == "file"
    )
    digest = sha256_bytes(
        json.dumps(
            material,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    return digest, total_bytes


def change_threshold_reached(
    threshold_bytes: int,
    current_total_bytes: int,
    last_snapshot_bytes: int | None,
) -> bool:
    if threshold_bytes <= 0 or last_snapshot_bytes is None:
        return True
    return abs(int(current_total_bytes) - int(last_snapshot_bytes)) >= threshold_bytes


def sanitize_name(value: str) -> str:
    forbidden = '<>:"/\\|?*'
    result = "".join("_" if char in forbidden or ord(char) < 32 else char for char in value)
    result = result.strip(". ")
    return (result or "Plan")[:80]


def setup_logging(paths: AppPaths) -> None:
    handlers: list[logging.Handler] = [logging.FileHandler(paths.log_path, encoding="utf-8")]
    if sys.stderr is not None:  # a windowed (no-console) build has no stderr
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
        handlers=handlers,
        force=True,
    )

def set_read_only_best_effort(path: Path) -> None:
    try:
        if path.is_file():
            path.chmod(path.stat().st_mode & ~stat.S_IWRITE)
            return

        if path.is_dir():
            for root, directories, files in os.walk(path):
                root_path = Path(root)

                for name in files:
                    target = root_path / name
                    try:
                        target.chmod(target.stat().st_mode & ~stat.S_IWRITE)
                    except OSError:
                        logging.debug(
                            "Unable to set file read-only: %s",
                            target,
                            exc_info=True,
                        )

                for name in directories:
                    target = root_path / name
                    try:
                        target.chmod(target.stat().st_mode & ~stat.S_IWRITE)
                    except OSError:
                        logging.debug(
                            "Unable to set directory read-only: %s",
                            target,
                            exc_info=True,
                        )

            path.chmod(path.stat().st_mode & ~stat.S_IWRITE)
    except OSError:
        logging.debug(
            "Unable to set artifact read-only: %s",
            path,
            exc_info=True,
        )


def set_writable_best_effort(path: Path) -> None:
    try:
        if path.is_file():
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
            return

        if path.is_dir():
            for root, directories, files in os.walk(path):
                root_path = Path(root)

                for name in files:
                    target = root_path / name
                    try:
                        target.chmod(target.stat().st_mode | stat.S_IWRITE)
                    except OSError:
                        logging.debug(
                            "Unable to make file writable: %s",
                            target,
                            exc_info=True,
                        )

                for name in directories:
                    target = root_path / name
                    try:
                        target.chmod(target.stat().st_mode | stat.S_IWRITE)
                    except OSError:
                        logging.debug(
                            "Unable to make directory writable: %s",
                            target,
                            exc_info=True,
                        )

            path.chmod(path.stat().st_mode | stat.S_IWRITE)
    except OSError:
        logging.debug(
            "Unable to make artifact writable: %s",
            path,
            exc_info=True,
        )


class PathPolicy:
    @staticmethod
    def _reject_special(text: str, storage: bool = False) -> None:
        role = "STORAGE" if storage else "SOURCE"
        value = text.strip()
        if not value:
            raise AppError(f"{role}_PATH_INVALID", "No path given.")
        if value.startswith("\\\\?\\") or value.startswith("\\\\.\\"):
            raise AppError(f"{role}_UNSUPPORTED_PATH", "System device paths are not supported.")
        if value.startswith("\\\\"):
            raise AppError(f"{role}_UNSUPPORTED_PATH", "Network UNC paths are not supported in this version.")

    @classmethod
    def validate_source(cls, text: str) -> SourceDescriptor:
        cls._reject_special(text)
        path = Path(text.strip()).expanduser()
        if not path.is_absolute():
            raise AppError("SOURCE_PATH_INVALID", "An absolute path to the source is required.")
        try:
            path = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise AppError("SOURCE_NOT_FOUND", "The specified source does not exist.", str(exc)) from exc
        except PermissionError as exc:
            raise AppError("SOURCE_ACCESS_DENIED", "No access to the source.", str(exc)) from exc
        if path.parent == path:
            raise AppError("SOURCE_ROOT_NOT_ALLOWED", "A drive root cannot be backed up.")
        if path.is_symlink():
            raise AppError("SOURCE_REPARSE_POINT_BLOCKED", "Symbolic links and junctions cannot be chosen as a source.")
        if path.is_file():
            return SourceDescriptor(path, SourceKind.FILE)
        if path.is_dir():
            return SourceDescriptor(path, SourceKind.FOLDER)
        raise AppError("SOURCE_UNSUPPORTED_TYPE", "The source must be a regular file or folder.")

    @classmethod
    def validate_storage(cls, text: str) -> Path:
        cls._reject_special(text, storage=True)
        path = Path(text.strip()).expanduser()
        if not path.is_absolute():
            raise AppError("STORAGE_PATH_INVALID", "An absolute path to the storage is required.")
        try:
            path.mkdir(parents=True, exist_ok=True)
            path = path.resolve(strict=True)
            probe = path / f".amvibackup-probe-{uuid.uuid4().hex}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return path
        except PermissionError as exc:
            raise AppError("STORAGE_ACCESS_DENIED", "No permission to write to the selected storage.", str(exc)) from exc
        except OSError as exc:
            raise AppError("STORAGE_INITIALIZATION_FAILED", "Could not prepare the storage folder.", str(exc)) from exc

    @staticmethod
    def inside(child: Path, parent: Path) -> bool:
        try:
            child.resolve(strict=False).relative_to(parent.resolve(strict=False))
            return True
        except ValueError:
            return False

    @classmethod
    def validate_topology(cls, source: SourceDescriptor, storage: Path) -> None:
        if source.kind == SourceKind.FOLDER:
            if storage == source.path or cls.inside(storage, source.path) or cls.inside(source.path, storage):
                raise AppError(
                    "STORAGE_TOPOLOGY_INVALID",
                    "A source folder and the storage must not be inside each other.",
                )
        elif storage == source.path:
            raise AppError("STORAGE_TOPOLOGY_INVALID", "The storage cannot be the same as the source file.")

    @classmethod
    def safe_join_under(cls, root: Path, relative_path: str) -> Path:
        normalized = relative_path.replace("\\", "/")
        parts = normalized.split("/")
        if not normalized or normalized.startswith("/") or ".." in parts or ":" in parts[0]:
            raise AppError("MANIFEST_INVALID", "The manifest contains an unsafe path.")
        candidate = (root / Path(normalized)).resolve(strict=False)
        if not cls.inside(candidate, root):
            raise AppError("MANIFEST_INVALID", "A manifest path points outside the snapshot.")
        return candidate


class Database:
    _write_lock = threading.RLock()

    def __init__(self, path: Path, journal_mode: str = "WAL") -> None:
        self.path = path
        self.journal_mode = journal_mode

    def connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA journal_mode={self.journal_mode}")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            return connection
        except sqlite3.Error as exc:
            raise AppError("DATABASE_OPEN_FAILED", "Could not open the application database.", str(exc)) from exc

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

    def checkpoint_wal(self) -> None:
        try:
            with self.read_connection() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            logging.debug("WAL checkpoint was unavailable", exc_info=True)

    def close_for_cleanup(self) -> None:
        self.checkpoint_wal()

    def migrate(self) -> None:
        with self._migration_transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS SchemaMigrations "
                "(version INTEGER PRIMARY KEY, applied_at_utc_ms INTEGER NOT NULL)"
            )
            latest = conn.execute("SELECT MAX(version) FROM SchemaMigrations").fetchone()[0]
            if latest is not None and latest > DB_SCHEMA_VERSION:
                raise AppError("DATABASE_VERSION_TOO_NEW",
                               "The database was created by a newer version of AmviBackUp; update the application.")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS AppSettings "
                "(key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at_utc_ms INTEGER NOT NULL)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS StorageRoots (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    root_path TEXT NOT NULL UNIQUE,
                    created_at_utc_ms INTEGER NOT NULL,
                    last_seen_at_utc_ms INTEGER NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS BackupPlans (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL,
                    source_kind TEXT NOT NULL CHECK(source_kind IN ('file','folder')),
                    source_identity_json TEXT NULL,
                    storage_mode TEXT NOT NULL DEFAULT 'direct' CHECK(storage_mode IN ('direct','zip')),
                    storage_root_id TEXT NOT NULL REFERENCES StorageRoots(id),
                    state TEXT NOT NULL CHECK(state IN ('enabled','paused','archived')),
                    keep_last_n INTEGER NOT NULL DEFAULT 5 CHECK(keep_last_n BETWEEN 1 AND 1000),
                    retention_mode TEXT NOT NULL DEFAULT 'count' CHECK(retention_mode IN ('count','time')),
                    retention_daily INTEGER NOT NULL DEFAULT 0 CHECK(retention_daily BETWEEN 0 AND 1000),
                    retention_weekly INTEGER NOT NULL DEFAULT 0 CHECK(retention_weekly BETWEEN 0 AND 1000),
                    retention_monthly INTEGER NOT NULL DEFAULT 0 CHECK(retention_monthly BETWEEN 0 AND 1000),
                    last_success_snapshot_id TEXT NULL,
                    on_change_enabled INTEGER NOT NULL DEFAULT 0 CHECK(on_change_enabled IN (0,1)),
                    change_debounce_seconds INTEGER NOT NULL DEFAULT 5 CHECK(change_debounce_seconds BETWEEN 1 AND 3600),
                    min_auto_interval_minutes INTEGER NOT NULL DEFAULT 10 CHECK(min_auto_interval_minutes BETWEEN 0 AND 10080),
                    change_threshold_bytes INTEGER NOT NULL DEFAULT 0 CHECK(change_threshold_bytes >= 0),
                    on_schedule_enabled INTEGER NOT NULL DEFAULT 0 CHECK(on_schedule_enabled IN (0,1)),
                    schedule_interval_minutes INTEGER NOT NULL DEFAULT 60 CHECK(schedule_interval_minutes BETWEEN 1 AND 10080),
                    quiet_hours_enabled INTEGER NOT NULL DEFAULT 0 CHECK(quiet_hours_enabled IN (0,1)),
                    quiet_hours_start_local TEXT NULL,
                    quiet_hours_end_local TEXT NULL,
                    manual_allowed_quiet_hours INTEGER NOT NULL DEFAULT 1 CHECK(manual_allowed_quiet_hours IN (0,1)),
                    last_auto_backup_at_utc_ms INTEGER NULL,
                    pending_trigger_mask INTEGER NOT NULL DEFAULT 0,
                    pending_trigger_since_utc_ms INTEGER NULL,
                    last_source_scan_fingerprint TEXT NOT NULL DEFAULT '',
                    created_at_utc_ms INTEGER NOT NULL,
                    updated_at_utc_ms INTEGER NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS Operations (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled','needs_recovery')),
                    plan_id TEXT NULL REFERENCES BackupPlans(id),
                    snapshot_id TEXT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT NULL,
                    error_message TEXT NULL,
                    technical_details TEXT NULL,
                    created_at_utc_ms INTEGER NOT NULL,
                    started_at_utc_ms INTEGER NULL,
                    finished_at_utc_ms INTEGER NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS Snapshots (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES BackupPlans(id) ON DELETE CASCADE,
                    operation_id TEXT NOT NULL REFERENCES Operations(id),
                    sequence_no INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('creating','committed','corrupted','deleting','deleted','needs_recovery')),
                    storage_mode TEXT NOT NULL DEFAULT 'direct' CHECK(storage_mode IN ('direct','zip')),
                    created_at_utc_ms INTEGER NOT NULL,
                    artifact_relpath TEXT NOT NULL,
                    manifest_relpath TEXT NOT NULL,
                    logical_size_bytes INTEGER NOT NULL DEFAULT 0,
                    stored_size_bytes INTEGER NOT NULL DEFAULT 0,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    manifest_sha256 TEXT NOT NULL DEFAULT '',
                    verification_state TEXT NOT NULL DEFAULT 'not_verified' CHECK(verification_state IN ('not_verified','passed','failed')),
                    verified_at_utc_ms INTEGER NULL,
                    pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
                    UNIQUE(plan_id, sequence_no)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS ActivityLog (
                    id TEXT PRIMARY KEY,
                    created_at_utc_ms INTEGER NOT NULL,
                    level TEXT NOT NULL CHECK(level IN ('info','warning','error')),
                    plan_id TEXT NULL REFERENCES BackupPlans(id),
                    operation_id TEXT NULL REFERENCES Operations(id),
                    event_code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS Issues (
                    id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    plan_id TEXT NULL REFERENCES BackupPlans(id) ON DELETE CASCADE,
                    severity TEXT NOT NULL CHECK(severity IN ('warning','error','critical')),
                    code TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','resolved','dismissed')),
                    first_seen_at_utc_ms INTEGER NOT NULL,
                    last_seen_at_utc_ms INTEGER NOT NULL,
                    resolved_at_utc_ms INTEGER NULL
                )"""
            )

            if "details_json" not in self.columns(conn, "ActivityLog"):
                conn.execute("ALTER TABLE ActivityLog ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
            if "source_fingerprint" not in self.columns(conn, "Snapshots"):
                conn.execute("ALTER TABLE Snapshots ADD COLUMN source_fingerprint TEXT NOT NULL DEFAULT ''")
            if "storage_mode" not in self.columns(conn, "BackupPlans"):
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN storage_mode TEXT NOT NULL DEFAULT 'direct' "
                    "CHECK(storage_mode IN ('direct','zip'))"
                )

            if "storage_mode" not in self.columns(conn, "Snapshots"):
                conn.execute(
                    "ALTER TABLE Snapshots "
                    "ADD COLUMN storage_mode TEXT NOT NULL DEFAULT 'direct' "
                    "CHECK(storage_mode IN ('direct','zip'))"
                )

            backup_plan_columns = self.columns(conn, "BackupPlans")
            if "retention_mode" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans ADD COLUMN retention_mode TEXT "
                    "NOT NULL DEFAULT 'count' CHECK(retention_mode IN ('count','time'))"
                )
            if "retention_daily" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans ADD COLUMN retention_daily INTEGER "
                    "NOT NULL DEFAULT 0 CHECK(retention_daily BETWEEN 0 AND 1000)"
                )
            if "retention_weekly" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans ADD COLUMN retention_weekly INTEGER "
                    "NOT NULL DEFAULT 0 CHECK(retention_weekly BETWEEN 0 AND 1000)"
                )
            if "retention_monthly" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans ADD COLUMN retention_monthly INTEGER "
                    "NOT NULL DEFAULT 0 CHECK(retention_monthly BETWEEN 0 AND 1000)"
                )

            backup_plan_columns = self.columns(conn, "BackupPlans")
            if "source_identity_json" not in backup_plan_columns:
                conn.execute("ALTER TABLE BackupPlans ADD COLUMN source_identity_json TEXT NULL")

            if "on_change_enabled" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN on_change_enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(on_change_enabled IN (0,1))"
                )

            if "change_debounce_seconds" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN change_debounce_seconds INTEGER NOT NULL DEFAULT 5 "
                    "CHECK(change_debounce_seconds BETWEEN 1 AND 3600)"
                )

            if "min_auto_interval_minutes" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN min_auto_interval_minutes INTEGER NOT NULL DEFAULT 10 "
                    "CHECK(min_auto_interval_minutes BETWEEN 0 AND 10080)"
                )

            if "change_threshold_bytes" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN change_threshold_bytes INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(change_threshold_bytes >= 0)"
                )

            if "on_schedule_enabled" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN on_schedule_enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(on_schedule_enabled IN (0,1))"
                )

            if "schedule_interval_minutes" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN schedule_interval_minutes INTEGER NOT NULL DEFAULT 60 "
                    "CHECK(schedule_interval_minutes BETWEEN 1 AND 10080)"
                )

            if "quiet_hours_enabled" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN quiet_hours_enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(quiet_hours_enabled IN (0,1))"
                )

            if "quiet_hours_start_local" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN quiet_hours_start_local TEXT NULL"
                )

            if "quiet_hours_end_local" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN quiet_hours_end_local TEXT NULL"
                )

            if "manual_allowed_quiet_hours" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN manual_allowed_quiet_hours INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(manual_allowed_quiet_hours IN (0,1))"
                )

            if "last_auto_backup_at_utc_ms" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN last_auto_backup_at_utc_ms INTEGER NULL"
                )

            if "pending_trigger_mask" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN pending_trigger_mask INTEGER NOT NULL DEFAULT 0"
                )

            if "pending_trigger_since_utc_ms" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN pending_trigger_since_utc_ms INTEGER NULL"
                )

            if "last_source_scan_fingerprint" not in backup_plan_columns:
                conn.execute(
                    "ALTER TABLE BackupPlans "
                    "ADD COLUMN last_source_scan_fingerprint TEXT NOT NULL DEFAULT ''"
                )

            issues_schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='Issues'"
            ).fetchone()

            if issues_schema and "'dismissed'" not in issues_schema["sql"]:
                conn.execute("ALTER TABLE Issues RENAME TO Issues_legacy")
                conn.execute(
                    """CREATE TABLE Issues (
                        id TEXT PRIMARY KEY,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        plan_id TEXT NULL REFERENCES BackupPlans(id) ON DELETE CASCADE,
                        severity TEXT NOT NULL
                            CHECK(severity IN ('warning','error','critical')),
                        code TEXT NOT NULL,
                        title TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        state TEXT NOT NULL
                            CHECK(state IN ('active','resolved','dismissed')),
                        first_seen_at_utc_ms INTEGER NOT NULL,
                        last_seen_at_utc_ms INTEGER NOT NULL,
                        resolved_at_utc_ms INTEGER NULL
                    )"""
                )
                conn.execute(
                    """INSERT INTO Issues
                    (id,dedupe_key,plan_id,severity,code,title,detail,state,
                     first_seen_at_utc_ms,last_seen_at_utc_ms,resolved_at_utc_ms)
                    SELECT id,dedupe_key,plan_id,severity,code,title,detail,state,
                           first_seen_at_utc_ms,last_seen_at_utc_ms,resolved_at_utc_ms
                    FROM Issues_legacy"""
                )
                conn.execute("DROP TABLE Issues_legacy")

            conn.execute(
                """CREATE TABLE IF NOT EXISTS PlanDeletionJobs (
                    plan_id TEXT PRIMARY KEY REFERENCES BackupPlans(id) ON DELETE CASCADE,
                    operation_id TEXT NOT NULL REFERENCES Operations(id),
                    inventory_json TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    created_at_utc_ms INTEGER NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS RetiredPlans (
                    plan_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    deleted_at_utc_ms INTEGER NOT NULL,
                    operation_id TEXT NOT NULL REFERENCES Operations(id)
                )"""
            )
            self._upgrade_operations_type_constraint(conn)
            self._verify_operation_types(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS ix_plans_state ON BackupPlans(state)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_operations_state_time ON Operations(state, created_at_utc_ms)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_snapshots_plan_sequence ON Snapshots(plan_id, sequence_no DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_activity_time ON ActivityLog(created_at_utc_ms DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_issues_active ON Issues(state, severity, last_seen_at_utc_ms DESC)")
            conn.execute(
                "INSERT OR IGNORE INTO SchemaMigrations(version, applied_at_utc_ms) VALUES (?, ?)",
                (DB_SCHEMA_VERSION, utc_ms()),
            )
    def _backup_before_schema_upgrade(self, conn: sqlite3.Connection) -> Path | None:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if not tables:
            return None
        version = 0
        if "SchemaMigrations" in tables:
            version = conn.execute("SELECT MAX(version) FROM SchemaMigrations").fetchone()[0] or 0
        if version > DB_SCHEMA_VERSION:
            return None  # migrate() will refuse a newer database.
        operations = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='Operations'"
        ).fetchone()
        obsolete_type_check = operations is not None and re.search(
            r'CHECK\s*\(\s*(?:type|"type"|`type`|\[type\])\s+IN\s*\(',
            operations[0], flags=re.IGNORECASE,
        ) is not None
        missing_schema = any(
            expected - self.columns(conn, table)
            for table, expected in self._required_schema_columns().items()
        )
        if version >= DB_SCHEMA_VERSION and not obsolete_type_check and not missing_schema:
            return None
        target: Path | None = None
        destination: sqlite3.Connection | None = None
        try:
            folder = self.path.parent / "recovery" / "database-backups"
            folder.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = folder / f"schema-{version}-before-{DB_SCHEMA_VERSION}-{timestamp}-{uuid.uuid4().hex}.sqlite3"
            target.touch(exist_ok=False)
            destination = sqlite3.connect(target, timeout=5)
            conn.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise AppError("DATABASE_BACKUP_FAILED",
                               "The database copy made before the upgrade failed the integrity check. The upgrade was cancelled.")
            logging.info("Pre-migration database backup: %s", target)
            return target
        except (OSError, sqlite3.Error) as exc:
            raise AppError(
                "DATABASE_BACKUP_FAILED",
                "Could not save a database copy before the upgrade. "
                "The upgrade was cancelled; check free space and permissions.",
                str(exc),
            ) from exc
        finally:
            if destination is not None:
                destination.close()

    @contextmanager
    def _migration_transaction(self) -> Iterator[sqlite3.Connection]:
        # Table rebuilds follow SQLite's generalized ALTER TABLE procedure.
        # FK enforcement is disabled only on this private connection, before BEGIN.
        with self._write_lock:
            connection = self.connect()
            try:
                self._backup_before_schema_upgrade(connection)
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise AppError(
                        "DATABASE_MIGRATION_FAILED",
                        "The database upgrade breaks data relations; the changes were rolled back.",
                    )
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise AppError("DATABASE_MIGRATION_FAILED",
                                   "The database upgrade failed the integrity check.", integrity)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                try:
                    connection.execute("PRAGMA foreign_keys=ON")
                finally:
                    connection.close()

    def _upgrade_operations_type_constraint(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='Operations'"
        ).fetchone()
        if row is None:
            raise AppError("DATABASE_MIGRATION_FAILED", "The Operations table was not found.")

        ident = r'(?:type|"type"|`type`|\[type\])'
        literal = r"'(?:[^']|'')*'"
        obsolete = (
            rf'\b({ident}\s+TEXT\s+NOT\s+NULL)\s+'
            rf'CHECK\s*\(\s*{ident}\s+IN\s*\(\s*'
            rf'{literal}(?:\s*,\s*{literal})*\s*\)\s*\)'
        )
        revised, count = re.subn(obsolete, r'\1', row["sql"], flags=re.IGNORECASE)
        if count == 0:
            return
        if count != 1:
            raise AppError("DATABASE_MIGRATION_FAILED", "Ambiguous Operations schema.")

        revised, headers = re.subn(
            r'^\s*CREATE\s+TABLE\s+(?:"Operations"|`Operations`|\[Operations\]|Operations)\s*\(',
            'CREATE TABLE "Operations_upgrade" (', revised,
            count=1, flags=re.IGNORECASE,
        )
        if headers != 1:
            raise AppError("DATABASE_MIGRATION_FAILED", "Unknown Operations table format.")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='Operations_upgrade'").fetchone():
            raise AppError("DATABASE_MIGRATION_FAILED", "The migration table name is already in use.")

        # Drop/recreate views and triggers too: they may reference Operations
        # while its old table is temporarily absent. All DDL stays transactional.
        dependents = list(conn.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND "
            "(type IN ('view','trigger') OR (type='index' AND tbl_name='Operations')) "
            "ORDER BY CASE type WHEN 'view' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name"
        ))
        def quoted(name: str) -> str:
            return '"' + name.replace('"', '""') + '"'

        for dependent in reversed(dependents):
            if dependent["type"] in {"view", "trigger"}:
                conn.execute(f'DROP {dependent["type"].upper()} {quoted(dependent["name"])}')

        conn.execute(revised)
        fields = [row["name"] for row in conn.execute('PRAGMA table_xinfo("Operations")')
                  if row["hidden"] == 0]
        columns = ','.join(quoted(name) for name in fields)
        conn.execute(f'INSERT INTO Operations_upgrade ({columns}) SELECT {columns} FROM Operations')
        conn.execute('DROP TABLE Operations')
        conn.execute('ALTER TABLE Operations_upgrade RENAME TO Operations')
        for dependent in dependents:
            conn.execute(dependent["sql"])

    @staticmethod
    def _verify_operation_types(conn: sqlite3.Connection) -> None:
        # Exercise the actual constraint without leaving journal rows or
        # transactional trigger effects. A column-only check cannot detect it.
        conn.execute("SAVEPOINT verify_operation_types")
        try:
            for kind in OperationType:
                conn.execute(
                    "INSERT INTO Operations(id,type,state,created_at_utc_ms) VALUES (?,?,?,?)",
                    (str(uuid.uuid4()), kind.value, "cancelled", utc_ms()),
                )
        except sqlite3.Error as exc:
            raise AppError(
                "DATABASE_MIGRATION_FAILED",
                "The operations log schema is incompatible with this version of the application. "
                "The database upgrade did not finish; check the log.",
                str(exc),
            ) from exc
        finally:
            conn.execute("ROLLBACK TO verify_operation_types")
            conn.execute("RELEASE verify_operation_types")


    @staticmethod
    def _required_schema_columns() -> dict[str, set[str]]:
        return {
            "PlanDeletionJobs": {"plan_id", "operation_id", "inventory_json", "preview_json", "created_at_utc_ms"},
            "RetiredPlans": {"plan_id", "display_name", "source_path", "storage_path", "deleted_at_utc_ms", "operation_id"},
            "ActivityLog": {
                "id", "created_at_utc_ms", "level", "plan_id", "operation_id",
                "event_code", "message", "details_json",
            },
            "BackupPlans": {
                "id",
                "display_name",
                "source_path",
                "source_kind", "source_identity_json",
                "storage_mode",
                "storage_root_id",
                "state",
                "keep_last_n",
                "retention_mode",
                "retention_daily",
                "retention_weekly",
                "retention_monthly",
                "on_change_enabled",
                "change_debounce_seconds",
                "min_auto_interval_minutes",
                "change_threshold_bytes",
                "on_schedule_enabled",
                "schedule_interval_minutes",
                "quiet_hours_enabled",
                "quiet_hours_start_local",
                "quiet_hours_end_local",
                "manual_allowed_quiet_hours",
                "last_auto_backup_at_utc_ms",
                "pending_trigger_mask",
                "pending_trigger_since_utc_ms",
                "last_source_scan_fingerprint",
            },
            "Snapshots": {
                "id", "plan_id", "operation_id", "sequence_no", "state", "storage_mode",
                "source_fingerprint", "manifest_sha256",
            },
            "Operations": {"id", "type", "state", "plan_id", "payload_json"},
            "Issues": {
                "id", "dedupe_key", "plan_id", "severity", "code", "title", "detail",
                "state", "first_seen_at_utc_ms", "last_seen_at_utc_ms", "resolved_at_utc_ms",
            },
        }

    def verify_contract(self) -> None:
        required = self._required_schema_columns()
        with self.read_connection() as conn:
            for table, expected in required.items():
                missing = expected - self.columns(conn, table)
                if missing:
                    raise AppError(
                        "DATABASE_MIGRATION_FAILED",
                        f"The database is missing required fields of table {table}.",
                        f"Missing: {sorted(missing)}",
                    )
            self._verify_operation_types(conn)
            for table in ("PlanDeletionJobs", "RetiredPlans"):
                fields = {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}
                if fields["plan_id"]["pk"] != 1:
                    raise AppError("DATABASE_MIGRATION_FAILED", f"Invalid key in {table}.")
                keys = {(row["from"], row["table"], row["to"], row["on_delete"])
                        for row in conn.execute(f"PRAGMA foreign_key_list({table})")}
                if not any(key[:3] == ("operation_id", "Operations", "id") for key in keys):
                    raise AppError("DATABASE_MIGRATION_FAILED", f"Missing operation foreign key in {table}.")
                if table == "PlanDeletionJobs" and ("plan_id", "BackupPlans", "id", "CASCADE") not in keys:
                    raise AppError("DATABASE_MIGRATION_FAILED", "Missing foreign key of the deletion plan.")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise AppError("DATABASE_INTEGRITY_FAILED", "SQLite foreign key violations found.")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise AppError("DATABASE_INTEGRITY_FAILED", "The database integrity check failed.", integrity)


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _summary(row: sqlite3.Row) -> PlanSummary:
        return PlanSummary(
            row["id"],
            row["display_name"],
            row["source_path"],
            row["state"],
            row["storage_path"],
            int(row["keep_last_n"]),
            int(row["snapshot_count"] or 0),
            row["last_snapshot_at_utc_ms"],
            row["last_operation_state"],
            bool(row["on_change_enabled"]),
            bool(row["on_schedule_enabled"]),
            int(row["pending_trigger_mask"]),
            row["last_auto_backup_at_utc_ms"],
        )

    @staticmethod
    def _details(row: sqlite3.Row) -> PlanDetails:
        return PlanDetails(
            row["id"],
            row["display_name"],
            row["note"],
            row["source_path"],
            row["source_kind"],
            row["storage_mode"],
            row["state"],
            row["storage_path"],
            row["storage_root_id"],
            int(row["keep_last_n"]),
            row["last_success_snapshot_id"],
            int(row["snapshot_count"] or 0),
            row["last_snapshot_at_utc_ms"],
            row["last_operation_state"],
            bool(row["on_change_enabled"]),
            int(row["change_debounce_seconds"]),
            int(row["min_auto_interval_minutes"]),
            bool(row["on_schedule_enabled"]),
            int(row["schedule_interval_minutes"]),
            bool(row["quiet_hours_enabled"]),
            row["quiet_hours_start_local"],
            row["quiet_hours_end_local"],
            bool(row["manual_allowed_quiet_hours"]),
            row["last_auto_backup_at_utc_ms"],
            int(row["pending_trigger_mask"]),
            row["pending_trigger_since_utc_ms"],
            row["last_source_scan_fingerprint"],
            row["source_identity_json"],
            row["retention_mode"],
            int(row["retention_daily"]),
            int(row["retention_weekly"]),
            int(row["retention_monthly"]),
            int(row["change_threshold_bytes"]),
        )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> SnapshotInfo:
        keys = set(row.keys())
        return SnapshotInfo(
            row["id"],
            row["plan_id"],
            row["operation_id"],
            int(row["sequence_no"]),
            row["state"],
            row["storage_mode"],
            int(row["created_at_utc_ms"]),
            row["artifact_relpath"],
            row["manifest_relpath"],
            int(row["logical_size_bytes"]),
            int(row["stored_size_bytes"]),
            int(row["file_count"]),
            row["source_fingerprint"],
            row["manifest_sha256"],
            row["verification_state"],
            row["verified_at_utc_ms"],
            bool(row["pinned"]),
            row["display_name"] if "display_name" in keys else "",
            row["source_path"] if "source_path" in keys else "",
            row["source_kind"] if "source_kind" in keys else "",
            row["plan_state"] if "plan_state" in keys else "",
            row["storage_path"] if "storage_path" in keys else "",
        )

    @staticmethod
    def _plan_query(where: str) -> str:
        return f"""SELECT p.*, s.root_path AS storage_path,
            (SELECT COUNT(*) FROM Snapshots x WHERE x.plan_id=p.id AND x.state='committed') AS snapshot_count,
            (SELECT MAX(created_at_utc_ms) FROM Snapshots x WHERE x.plan_id=p.id AND x.state='committed') AS last_snapshot_at_utc_ms,
            (SELECT state FROM Operations o WHERE o.plan_id=p.id ORDER BY o.created_at_utc_ms DESC LIMIT 1) AS last_operation_state
            FROM BackupPlans p
            JOIN StorageRoots s ON s.id=p.storage_root_id
            {where}"""

    @staticmethod
    def validate_trigger_policy(policy: TriggerPolicy) -> TriggerPolicy:
        if not (
            MIN_CHANGE_DEBOUNCE_SECONDS
            <= policy.change_debounce_seconds
            <= MAX_CHANGE_DEBOUNCE_SECONDS
        ):
            raise AppError(
                "TRIGGER_POLICY_INVALID",
                "The delay after a change must be from "
                f"{MIN_CHANGE_DEBOUNCE_SECONDS} to "
                f"{MAX_CHANGE_DEBOUNCE_SECONDS} seconds.",
            )

        if not (
            MIN_MIN_AUTO_INTERVAL_MINUTES
            <= policy.min_auto_interval_minutes
            <= MAX_MIN_AUTO_INTERVAL_MINUTES
        ):
            raise AppError(
                "TRIGGER_POLICY_INVALID",
                "The minimum interval between automatic backups must be from "
                f"{MIN_MIN_AUTO_INTERVAL_MINUTES} to "
                f"{MAX_MIN_AUTO_INTERVAL_MINUTES} minutes.",
            )

        if not (
            MIN_SCHEDULE_INTERVAL_MINUTES
            <= policy.schedule_interval_minutes
            <= MAX_SCHEDULE_INTERVAL_MINUTES
        ):
            raise AppError(
                "TRIGGER_POLICY_INVALID",
                "The schedule interval must be from "
                f"{MIN_SCHEDULE_INTERVAL_MINUTES} to "
                f"{MAX_SCHEDULE_INTERVAL_MINUTES} minutes.",
            )

        quiet_start = validate_local_time_text(
            policy.quiet_hours_start_local
        )
        quiet_end = validate_local_time_text(
            policy.quiet_hours_end_local
        )

        if policy.quiet_hours_enabled:
            if quiet_start is None or quiet_end is None:
                raise AppError(
                    "TRIGGER_POLICY_INVALID",
                    "When quiet hours are enabled, set the start and end time.",
                )

            if quiet_start == quiet_end:
                raise AppError(
                    "TRIGGER_POLICY_INVALID",
                    "The start and end of quiet hours cannot be the same.",
                )
        else:
            quiet_start = None
            quiet_end = None

        threshold = int(getattr(policy, "change_threshold_bytes", 0) or 0)
        if not 0 <= threshold <= MAX_CHANGE_THRESHOLD_BYTES:
            raise AppError(
                "TRIGGER_POLICY_INVALID",
                "The change size threshold must be from 0 bytes to 1 TB.",
            )

        return TriggerPolicy(
            on_change_enabled=bool(policy.on_change_enabled),
            change_debounce_seconds=int(
                policy.change_debounce_seconds
            ),
            min_auto_interval_minutes=int(
                policy.min_auto_interval_minutes
            ),
            on_schedule_enabled=bool(
                policy.on_schedule_enabled
            ),
            schedule_interval_minutes=int(
                policy.schedule_interval_minutes
            ),
            quiet_hours_enabled=bool(
                policy.quiet_hours_enabled
            ),
            quiet_hours_start_local=quiet_start,
            quiet_hours_end_local=quiet_end,
            manual_allowed_quiet_hours=bool(
                policy.manual_allowed_quiet_hours
            ),
            change_threshold_bytes=threshold,
        )

    @staticmethod
    def _policy_from_row(row: sqlite3.Row) -> TriggerPolicy:
        return TriggerPolicy(
            on_change_enabled=bool(row["on_change_enabled"]),
            change_debounce_seconds=int(
                row["change_debounce_seconds"]
            ),
            min_auto_interval_minutes=int(
                row["min_auto_interval_minutes"]
            ),
            on_schedule_enabled=bool(
                row["on_schedule_enabled"]
            ),
            schedule_interval_minutes=int(
                row["schedule_interval_minutes"]
            ),
            quiet_hours_enabled=bool(
                row["quiet_hours_enabled"]
            ),
            quiet_hours_start_local=row["quiet_hours_start_local"],
            quiet_hours_end_local=row["quiet_hours_end_local"],
            manual_allowed_quiet_hours=bool(
                row["manual_allowed_quiet_hours"]
            ),
            change_threshold_bytes=(
                int(row["change_threshold_bytes"])
                if "change_threshold_bytes" in row.keys() else 0
            ),
        )

    def activity(
        self,
        level: str,
        code: str,
        message: str,
        plan_id: str | None = None,
        operation_id: str | None = None,
        details: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        values = (
            str(uuid.uuid4()), utc_ms(), level, plan_id, operation_id, code, message,
            json.dumps(details or {}, ensure_ascii=False),
        )
        sql = (
            "INSERT INTO ActivityLog "
            "(id,created_at_utc_ms,level,plan_id,operation_id,event_code,message,details_json) "
            "VALUES (?,?,?,?,?,?,?,?)"
        )
        if conn is not None:
            conn.execute(sql, values)
            return
        with self.db.transaction() as tx:
            tx.execute(sql, values)

    def get_setting(
        self,
        key: str,
        default: Any = None,
    ) -> Any:
        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM AppSettings WHERE key=?",
                (key,),
            ).fetchone()

            if not row:
                return default

            try:
                return json.loads(row["value_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise AppError(
                    "APP_SETTING_INVALID",
                    f"An application setting is corrupted: {key}",
                    str(exc),
                ) from exc

    def set_setting(
        self,
        key: str,
        value: Any,
    ) -> None:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO AppSettings
                (key,value_json,updated_at_utc_ms)
                VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at_utc_ms=excluded.updated_at_utc_ms""",
                (
                    key,
                    serialized,
                    utc_ms(),
                ),
            )

    def set_settings(
        self,
        values: dict[str, Any],
    ) -> None:
        if not values:
            return

        now = utc_ms()

        with self.db.transaction() as conn:
            for key, value in values.items():
                conn.execute(
                    """INSERT INTO AppSettings
                    (key,value_json,updated_at_utc_ms)
                    VALUES (?,?,?)
                    ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at_utc_ms=excluded.updated_at_utc_ms""",
                    (
                        key,
                        json.dumps(
                            value,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                )

    def get_bool_setting(
        self,
        key: str,
        default: bool,
    ) -> bool:
        value = self.get_setting(key, default)

        if not isinstance(value, bool):
            raise AppError(
                "APP_SETTING_INVALID",
                f"Setting {key} must be true or false.",
            )

        return value

    def get_or_create_storage(self, root: Path) -> str:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT id FROM StorageRoots WHERE root_path=?", (str(root),)).fetchone()
            if row:
                conn.execute("UPDATE StorageRoots SET last_seen_at_utc_ms=? WHERE id=?", (utc_ms(), row["id"]))
                return row["id"]
            storage_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO StorageRoots (id,display_name,root_path,created_at_utc_ms,last_seen_at_utc_ms) VALUES (?,?,?,?,?)",
                (storage_id, root.name or str(root), str(root), utc_ms(), utc_ms()),
            )
            return storage_id

    def storage_root_history(self, recent_limit: int = 3, popular_limit: int = 5) -> tuple[list[str], list[str]]:
        """Return (recently used, most used) storage folders; the second list excludes the first."""
        with self.db.read_connection() as conn:
            recent = [
                str(row[0]) for row in conn.execute(
                    "SELECT root_path FROM StorageRoots "
                    "ORDER BY last_seen_at_utc_ms DESC, rowid DESC LIMIT ?", (recent_limit,)
                ).fetchall()
            ]
            popular = [
                str(row[0]) for row in conn.execute(
                    "SELECT s.root_path FROM StorageRoots s "
                    "JOIN BackupPlans p ON p.storage_root_id = s.id "
                    "GROUP BY s.id HAVING COUNT(p.id) >= 2 "
                    "ORDER BY COUNT(p.id) DESC, MAX(s.last_seen_at_utc_ms) DESC LIMIT ?",
                    (popular_limit + recent_limit,)
                ).fetchall()
            ]
        return recent, [path for path in popular if path not in recent][:popular_limit]

    @staticmethod
    def default_trigger_policy() -> TriggerPolicy:
        return TriggerPolicy(
            on_change_enabled=False,
            change_debounce_seconds=DEFAULT_CHANGE_DEBOUNCE_SECONDS,
            min_auto_interval_minutes=DEFAULT_MIN_AUTO_INTERVAL_MINUTES,
            on_schedule_enabled=False,
            schedule_interval_minutes=DEFAULT_SCHEDULE_INTERVAL_MINUTES,
            quiet_hours_enabled=False,
            quiet_hours_start_local=None,
            quiet_hours_end_local=None,
            manual_allowed_quiet_hours=True,
        )

    @staticmethod
    def _write_trigger_policy(
        conn: sqlite3.Connection,
        plan_id: str,
        policy: TriggerPolicy,
        now_utc_ms: int,
    ) -> None:
        conn.execute(
            """UPDATE BackupPlans SET
            on_change_enabled=?,
            change_debounce_seconds=?,
            min_auto_interval_minutes=?,
            on_schedule_enabled=?,
            schedule_interval_minutes=?,
            quiet_hours_enabled=?,
            quiet_hours_start_local=?,
            quiet_hours_end_local=?,
            manual_allowed_quiet_hours=?,
            updated_at_utc_ms=?
            WHERE id=?""",
            (
                1 if policy.on_change_enabled else 0,
                policy.change_debounce_seconds,
                policy.min_auto_interval_minutes,
                1 if policy.on_schedule_enabled else 0,
                policy.schedule_interval_minutes,
                1 if policy.quiet_hours_enabled else 0,
                policy.quiet_hours_start_local,
                policy.quiet_hours_end_local,
                1 if policy.manual_allowed_quiet_hours else 0,
                now_utc_ms,
                plan_id,
            ),
        )
        conn.execute(
            "UPDATE BackupPlans SET change_threshold_bytes=? WHERE id=?",
            (int(policy.change_threshold_bytes), plan_id),
        )

    def create_plan(
        self,
        name: str,
        note: str,
        source: SourceDescriptor,
        storage_id: str,
        keep: int,
        storage_mode: str = StorageMode.DIRECT.value,
    ) -> str:
        if storage_mode not in {
            StorageMode.DIRECT.value,
            StorageMode.ZIP.value,
        }:
            raise AppError(
                "STORAGE_MODE_INVALID",
                f"Unsupported storage mode: {storage_mode}",
            )

        identity = confirmed_source_identity(source.path, source.kind.value)
        plan_id = str(uuid.uuid4())
        now = utc_ms()

        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO BackupPlans
                (id,display_name,note,source_path,source_kind,storage_mode,storage_root_id,
                 state,keep_last_n,last_success_snapshot_id,
                 on_change_enabled,change_debounce_seconds,min_auto_interval_minutes,
                 on_schedule_enabled,schedule_interval_minutes,
                 quiet_hours_enabled,quiet_hours_start_local,quiet_hours_end_local,
                 manual_allowed_quiet_hours,last_auto_backup_at_utc_ms,
                 pending_trigger_mask,pending_trigger_since_utc_ms,last_source_scan_fingerprint,
                 created_at_utc_ms,updated_at_utc_ms,source_identity_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id,
                    name,
                    note,
                    str(source.path),
                    source.kind.value,
                    storage_mode,
                    storage_id,
                    PlanState.ENABLED.value,
                    keep,
                    None,
                    0,
                    DEFAULT_CHANGE_DEBOUNCE_SECONDS,
                    DEFAULT_MIN_AUTO_INTERVAL_MINUTES,
                    0,
                    DEFAULT_SCHEDULE_INTERVAL_MINUTES,
                    0,
                    None,
                    None,
                    1,
                    None,
                    0,
                    None,
                    "",
                    now,
                    now,
                    identity,
                ),
            )
            self.activity(
                "info",
                "PLAN_CREATED",
                f"Plan '{name}' created ({storage_mode}).",
                plan_id,
                conn=conn,
            )

        return plan_id

    def set_plan_storage_mode(
        self,
        plan_id: str,
        storage_mode: str,
    ) -> None:
        if storage_mode not in {
            StorageMode.DIRECT.value,
            StorageMode.ZIP.value,
        }:
            raise AppError(
                "STORAGE_MODE_INVALID",
                f"Unsupported storage mode: {storage_mode}",
            )

        with self.db.transaction() as conn:
            row = conn.execute(
                """SELECT p.state, p.storage_mode,
                   (SELECT COUNT(*) FROM Snapshots
                    WHERE plan_id=p.id) AS snapshot_count
                   FROM BackupPlans p WHERE p.id=?""",
                (plan_id,),
            ).fetchone()

            if row is None:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")

            if row["state"] == PlanState.ARCHIVED.value:
                raise AppError(
                    "PLAN_ARCHIVED",
                    "The mode of an archived plan cannot be changed.",
                )

            if row["storage_mode"] == storage_mode:
                return

            if int(row["snapshot_count"]) > 0:
                raise AppError(
                    "STORAGE_MODE_CHANGE_BLOCKED",
                    "The storage mode cannot be changed after the "
                    "first version is created. Create a new plan.",
                )

            conn.execute(
                """UPDATE BackupPlans
                   SET storage_mode=?, updated_at_utc_ms=?
                   WHERE id=?""",
                (storage_mode, utc_ms(), plan_id),
            )
            self.activity(
                "info",
                "PLAN_STORAGE_MODE_CHANGED",
                f"Plan storage mode changed to {storage_mode}.",
                plan_id,
                conn=conn,
            )

    def update_plan(
        self,
        plan_id: str,
        name: str,
        note: str,
        source: SourceDescriptor,
        storage_id: str,
        keep: int,
        storage_mode: str,
    ) -> None:
        if storage_mode not in {
            StorageMode.DIRECT.value,
            StorageMode.ZIP.value,
        }:
            raise AppError(
                "STORAGE_MODE_INVALID",
                f"Unsupported storage mode: {storage_mode}",
            )

        identity = confirmed_source_identity(source.path, source.kind.value)
        with self.db.transaction() as conn:
            row = conn.execute(
                """SELECT state, storage_mode, storage_root_id, source_path, source_identity_json,
                (SELECT COUNT(*) FROM Snapshots WHERE plan_id=?) AS snapshot_count
                FROM BackupPlans WHERE id=?""",
                (plan_id, plan_id),
            ).fetchone()

            if not row:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")

            if row["state"] == PlanState.ARCHIVED.value:
                raise AppError("PLAN_ARCHIVED", "An archived plan cannot be changed.")

            if (
                row["storage_mode"] != storage_mode
                and int(row["snapshot_count"]) > 0
            ):
                raise AppError(
                    "STORAGE_MODE_CHANGE_BLOCKED",
                    "The storage mode cannot be changed after the first version is created. "
                    "Create a new plan for a different mode.",
                )

            if row["storage_root_id"] != storage_id and int(row["snapshot_count"]) > 0:
                raise AppError(
                    "STORAGE_CHANGE_BLOCKED",
                    "The storage cannot be changed after a version is created. Create a new plan.",
                )

            if row["source_path"] == str(source.path) and row["source_identity_json"] != identity \
                    and not (source.kind == SourceKind.FILE and file_replaced_in_place(source.path, row["source_identity_json"])):
                raise AppError("SOURCE_IDENTITY_UNCONFIRMED", "Confirm the new source object explicitly first.")
            conn.execute(
                """UPDATE BackupPlans SET
                display_name=?, note=?, source_path=?, source_kind=?, storage_mode=?,
                storage_root_id=?, keep_last_n=?, source_identity_json=?, updated_at_utc_ms=?
                WHERE id=?""",
                (
                    name,
                    note,
                    str(source.path),
                    source.kind.value,
                    storage_mode,
                    storage_id,
                    keep,
                    identity,
                    utc_ms(),
                    plan_id,
                ),
            )
            self.activity(
                "info",
                "PLAN_UPDATED",
                f"Plan '{name}' updated.",
                plan_id,
                conn=conn,
            )

    def latest_committed_logical_size(self, plan_id: str) -> int | None:
        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT logical_size_bytes FROM Snapshots WHERE plan_id=? AND state='committed' "
                "ORDER BY sequence_no DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def get_trigger_policy(self, plan_id: str) -> TriggerPolicy:
        with self.db.read_connection() as conn:
            row = conn.execute(
                """SELECT
                on_change_enabled,
                change_debounce_seconds,
                min_auto_interval_minutes,
                on_schedule_enabled,
                schedule_interval_minutes,
                quiet_hours_enabled,
                quiet_hours_start_local,
                quiet_hours_end_local,
                manual_allowed_quiet_hours,
                change_threshold_bytes
                FROM BackupPlans
                WHERE id=?""",
                (plan_id,),
            ).fetchone()

            if not row:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")

            return self._policy_from_row(row)

    def update_trigger_policy(
        self,
        plan_id: str,
        policy: TriggerPolicy,
    ) -> TriggerPolicy:
        normalized = self.validate_trigger_policy(policy)

        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM BackupPlans WHERE id=?",
                (plan_id,),
            ).fetchone()

            if not row:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")

            if row["state"] == PlanState.ARCHIVED.value:
                raise AppError(
                    "PLAN_ARCHIVED",
                    "The automation policy of an archived plan cannot be changed.",
                )

            conn.execute(
                """UPDATE BackupPlans SET
                on_change_enabled=?,
                change_debounce_seconds=?,
                min_auto_interval_minutes=?,
                on_schedule_enabled=?,
                schedule_interval_minutes=?,
                quiet_hours_enabled=?,
                quiet_hours_start_local=?,
                quiet_hours_end_local=?,
                manual_allowed_quiet_hours=?,
                updated_at_utc_ms=?
                WHERE id=?""",
                (
                    1 if normalized.on_change_enabled else 0,
                    normalized.change_debounce_seconds,
                    normalized.min_auto_interval_minutes,
                    1 if normalized.on_schedule_enabled else 0,
                    normalized.schedule_interval_minutes,
                    1 if normalized.quiet_hours_enabled else 0,
                    normalized.quiet_hours_start_local,
                    normalized.quiet_hours_end_local,
                    1 if normalized.manual_allowed_quiet_hours else 0,
                    utc_ms(),
                    plan_id,
                ),
            )
            conn.execute(
                "UPDATE BackupPlans SET change_threshold_bytes=? WHERE id=?",
                (normalized.change_threshold_bytes, plan_id),
            )

            self.activity(
                "info",
                "TRIGGER_POLICY_UPDATED",
                "Automatic backup settings updated.",
                plan_id,
                details={
                    "on_change_enabled": normalized.on_change_enabled,
                    "change_debounce_seconds": normalized.change_debounce_seconds,
                    "min_auto_interval_minutes": normalized.min_auto_interval_minutes,
                    "on_schedule_enabled": normalized.on_schedule_enabled,
                    "schedule_interval_minutes": normalized.schedule_interval_minutes,
                    "quiet_hours_enabled": normalized.quiet_hours_enabled,
                    "quiet_hours_start_local": normalized.quiet_hours_start_local,
                    "quiet_hours_end_local": normalized.quiet_hours_end_local,
                    "manual_allowed_quiet_hours": normalized.manual_allowed_quiet_hours,
                },
                conn=conn,
            )

        return normalized

    def save_plan_with_policy(
        self,
        plan_id: str | None,
        name: str,
        note: str,
        source: SourceDescriptor,
        storage_id: str,
        keep: int,
        storage_mode: str,
        policy: TriggerPolicy,
        retention_policy: RetentionPolicy | None = None,
        retention_preview_token: str | None = None,
    ) -> str:
        if storage_mode not in {StorageMode.DIRECT.value, StorageMode.ZIP.value}:
            raise AppError("STORAGE_MODE_INVALID", f"Unsupported storage mode: {storage_mode}")
        identity = confirmed_source_identity(source.path, source.kind.value)
        normalized_trigger = self.validate_trigger_policy(policy)
        normalized_retention = self.validate_retention_policy(
            retention_policy or RetentionPolicy(RETENTION_MODE_COUNT, keep, 0, 0, 0)
        )
        now = utc_ms()
        with self.db.transaction() as conn:
            if plan_id is None:
                saved_plan_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO BackupPlans
                    (id,display_name,note,source_path,source_kind,storage_mode,storage_root_id,
                     state,keep_last_n,retention_mode,retention_daily,retention_weekly,retention_monthly,
                     last_success_snapshot_id,on_change_enabled,change_debounce_seconds,
                     min_auto_interval_minutes,on_schedule_enabled,schedule_interval_minutes,
                     quiet_hours_enabled,quiet_hours_start_local,quiet_hours_end_local,
                     manual_allowed_quiet_hours,last_auto_backup_at_utc_ms,pending_trigger_mask,
                     pending_trigger_since_utc_ms,last_source_scan_fingerprint,created_at_utc_ms,
                     updated_at_utc_ms,source_identity_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (saved_plan_id,name,note,str(source.path),source.kind.value,storage_mode,storage_id,
                     PlanState.ENABLED.value,normalized_retention.keep_last_n,normalized_retention.mode,
                     normalized_retention.daily,normalized_retention.weekly,normalized_retention.monthly,None,
                     1 if normalized_trigger.on_change_enabled else 0,normalized_trigger.change_debounce_seconds,
                     normalized_trigger.min_auto_interval_minutes,1 if normalized_trigger.on_schedule_enabled else 0,
                     normalized_trigger.schedule_interval_minutes,1 if normalized_trigger.quiet_hours_enabled else 0,
                     normalized_trigger.quiet_hours_start_local,normalized_trigger.quiet_hours_end_local,
                     1 if normalized_trigger.manual_allowed_quiet_hours else 0,None,0,None,"",now,now,identity),
                )
                conn.execute("UPDATE BackupPlans SET change_threshold_bytes=? WHERE id=?",
                             (normalized_trigger.change_threshold_bytes, saved_plan_id))
                self.activity("info","PLAN_CREATED",f"Plan '{name}' created ({storage_mode}).",saved_plan_id,
                              details={"storage_mode":storage_mode,"retention_mode":normalized_retention.mode},conn=conn)
                return saved_plan_id
            row = conn.execute(
                """SELECT state,storage_mode,storage_root_id,source_path,source_identity_json,
                retention_mode,retention_daily,retention_weekly,retention_monthly,keep_last_n,
                (SELECT COUNT(*) FROM Snapshots WHERE plan_id=?) AS snapshot_count
                FROM BackupPlans WHERE id=?""", (plan_id,plan_id)).fetchone()
            if not row: raise AppError("PLAN_NOT_FOUND","Plan not found.")
            if row["state"] == PlanState.ARCHIVED.value: raise AppError("PLAN_ARCHIVED","An archived plan cannot be changed.")
            if row["storage_mode"] != storage_mode and int(row["snapshot_count"]) > 0:
                raise AppError("STORAGE_MODE_CHANGE_BLOCKED","The storage mode cannot be changed after the first version is created.")
            if row["storage_root_id"] != storage_id and int(row["snapshot_count"]) > 0:
                raise AppError("STORAGE_CHANGE_BLOCKED","The storage cannot be changed after a version is created.")
            if row["source_path"] == str(source.path) and row["source_identity_json"] != identity \
                    and not (source.kind == SourceKind.FILE and file_replaced_in_place(source.path, row["source_identity_json"])):
                raise AppError("SOURCE_IDENTITY_UNCONFIRMED","Confirm the new source object explicitly first.")
            old_retention = RetentionPolicy(row["retention_mode"],int(row["keep_last_n"]),
                                            int(row["retention_daily"]),int(row["retention_weekly"]),
                                            int(row["retention_monthly"]))
            changed = old_retention != normalized_retention
            if changed and int(row["snapshot_count"]) > 0:
                if not retention_preview_token:
                    raise AppError("RETENTION_PREVIEW_REQUIRED","Review the deletion list before changing retention.")
                snapshot_rows = list(conn.execute(
                    "SELECT * FROM Snapshots WHERE plan_id=? AND state!='deleted' ORDER BY sequence_no DESC",
                    (plan_id,)).fetchall())
                preview = self._retention_preview_from_rows(plan_id,normalized_retention,snapshot_rows,now)
                if preview.token != retention_preview_token:
                    raise AppError("RETENTION_PREVIEW_STALE","The history changed. Review the deletion list again.")
            conn.execute(
                """UPDATE BackupPlans SET display_name=?,note=?,source_path=?,source_kind=?,source_identity_json=?,
                storage_mode=?,storage_root_id=?,keep_last_n=?,retention_mode=?,retention_daily=?,retention_weekly=?,
                retention_monthly=?,updated_at_utc_ms=? WHERE id=?""",
                (name,note,str(source.path),source.kind.value,identity,storage_mode,storage_id,
                 normalized_retention.keep_last_n,normalized_retention.mode,normalized_retention.daily,
                 normalized_retention.weekly,normalized_retention.monthly,now,plan_id))
            self._write_trigger_policy(conn,plan_id,normalized_trigger,now)
            self.activity("info","PLAN_UPDATED",f"Plan '{name}' updated.",plan_id,
                          details={"storage_mode":storage_mode,"retention_mode":normalized_retention.mode},conn=conn)
            return plan_id

    @staticmethod
    def validate_retention_policy(policy: RetentionPolicy) -> RetentionPolicy:
        if policy.mode not in {RETENTION_MODE_COUNT, RETENTION_MODE_TIME}:
            raise AppError("RETENTION_POLICY_INVALID", "Choose a supported storage mode.")
        if not MIN_KEEP_LAST_N <= int(policy.keep_last_n) <= MAX_KEEP_LAST_N:
            raise AppError("RETENTION_POLICY_INVALID", "The number of versions must be from 1 to 1000.")
        values = (int(policy.daily), int(policy.weekly), int(policy.monthly))
        if any(value < 0 or value > MAX_RETENTION_PERIODS for value in values):
            raise AppError("RETENTION_POLICY_INVALID", "The number of calendar periods must be from 0 to 1000.")
        if policy.mode == RETENTION_MODE_TIME and not any(values):
            raise AppError("RETENTION_POLICY_INVALID", "For time-based retention set at least one period.")
        return RetentionPolicy(policy.mode, int(policy.keep_last_n), *values)

    @staticmethod
    def _retention_preview_from_rows(
        plan_id: str,
        policy: RetentionPolicy,
        rows: list[sqlite3.Row] | list[SnapshotInfo],
        now_utc_ms: int | None = None,
        clock_blocked: bool = False,
    ) -> RetentionPreview:
        normalized = Repository.validate_retention_policy(policy)
        snapshots = [
            row if isinstance(row, SnapshotInfo) else Repository._snapshot(row)
            for row in rows
            if (row.state if isinstance(row, SnapshotInfo) else row["state"]) == "committed"
        ]
        snapshots.sort(key=lambda item: item.sequence_no, reverse=True)
        now_ms = utc_ms() if now_utc_ms is None else int(now_utc_ms)
        protected: set[str] = {item.id for item in snapshots if item.pinned}
        if snapshots:
            protected.add(snapshots[0].id)

        count_candidates: list[SnapshotInfo] | None = None
        if normalized.mode == RETENTION_MODE_COUNT:
            # Legacy semantics are preserved: pinned snapshots count toward the
            # configured total, but can make the retained set larger than N.
            excess = max(0, len(snapshots) - normalized.keep_last_n)
            count_candidates = [
                item for item in reversed(snapshots)
                if item.id not in protected
            ][:excess]
            protected.update(
                item.id for item in snapshots
                if item not in count_candidates
            )
        else:
            future_limit = now_ms + RETENTION_FUTURE_TOLERANCE_SECONDS * 1000
            protected.update(item.id for item in snapshots if item.created_at_utc_ms > future_limit)
            # Time-based thinning only applies to older history: the newest N versions are always kept,
            # so several versions made during one day never replace each other.
            protected.update(item.id for item in snapshots[: normalized.keep_last_n])
            now_local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone()
            today = now_local.date()
            daily_keys = {(today - timedelta(days=index)).isoformat() for index in range(normalized.daily)}
            monday = today - timedelta(days=today.weekday())
            weekly_keys = {
                (monday - timedelta(weeks=index)).isocalendar()[:2]
                for index in range(normalized.weekly)
            }
            monthly_keys: set[tuple[int, int]] = set()
            year, month = today.year, today.month
            for _ in range(normalized.monthly):
                monthly_keys.add((year, month))
                month -= 1
                if month == 0:
                    month = 12
                    year -= 1
            selected_days: set[str] = set()
            selected_weeks: set[tuple[int, int]] = set()
            selected_months: set[tuple[int, int]] = set()
            for item in snapshots:
                local = datetime.fromtimestamp(
                    item.created_at_utc_ms / 1000,
                    tz=timezone.utc,
                ).astimezone()
                day_key = local.date().isoformat()
                week_key = local.date().isocalendar()[:2]
                month_key = (local.year, local.month)
                keep = False
                if day_key in daily_keys and day_key not in selected_days:
                    selected_days.add(day_key); keep = True
                if week_key in weekly_keys and week_key not in selected_weeks:
                    selected_weeks.add(week_key); keep = True
                if month_key in monthly_keys and month_key not in selected_months:
                    selected_months.add(month_key); keep = True
                if keep:
                    protected.add(item.id)

        candidates = (
            count_candidates
            if count_candidates is not None
            else [item for item in reversed(snapshots) if item.id not in protected]
        )
        material = {
            "plan_id": plan_id,
            "policy": [normalized.mode, normalized.keep_last_n, normalized.daily, normalized.weekly, normalized.monthly],
            "now_local_date": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone().date().isoformat(),
            "snapshots": [
                [item.id, item.sequence_no, item.state, item.created_at_utc_ms, item.pinned]
                for item in snapshots
            ],
        }
        token = sha256_bytes(json_text(material).encode("utf-8"))
        return RetentionPreview(
            plan_id,
            token,
            tuple(item.id for item in candidates),
            tuple(item.sequence_no for item in candidates),
            tuple(sorted(protected)),
            bool(clock_blocked),
        )

    def retention_preview(
        self,
        plan_id: str,
        policy: RetentionPolicy | None = None,
        now_utc_ms: int | None = None,
    ) -> RetentionPreview:
        plan = self.plan_details(plan_id)
        effective = policy or RetentionPolicy(
            plan.retention_mode,
            plan.keep_last_n,
            plan.retention_daily,
            plan.retention_weekly,
            plan.retention_monthly,
        )
        rows = self.snapshots(plan_id)
        blocked = bool(self.get_setting(SETTING_RETENTION_CLOCK_BLOCKED, False))
        return self._retention_preview_from_rows(plan_id, effective, rows, now_utc_ms, blocked)

    def mark_pending_trigger(
        self,
        plan_id: str,
        trigger_mask: int,
        now_utc_ms: int | None = None,
    ) -> int:
        allowed_mask = (
            TRIGGER_MASK_EVENT
            | TRIGGER_MASK_SCHEDULE
            | TRIGGER_MASK_STARTUP_CATCHUP
        )

        if trigger_mask <= 0 or trigger_mask & ~allowed_mask:
            raise AppError(
                "TRIGGER_MASK_INVALID",
                "An unsupported automatic trigger reason was passed.",
            )

        now = utc_ms() if now_utc_ms is None else int(now_utc_ms)

        with self.db.transaction() as conn:
            row = conn.execute(
                """SELECT
                state,
                pending_trigger_mask,
                pending_trigger_since_utc_ms
                FROM BackupPlans
                WHERE id=?""",
                (plan_id,),
            ).fetchone()

            if not row:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")

            if row["state"] != PlanState.ENABLED.value:
                return int(row["pending_trigger_mask"])

            previous_mask = int(row["pending_trigger_mask"])
            merged_mask = previous_mask | trigger_mask
            pending_since = (
                row["pending_trigger_since_utc_ms"]
                if previous_mask != 0
                else now
            )

            conn.execute(
                """UPDATE BackupPlans SET
                pending_trigger_mask=?,
                pending_trigger_since_utc_ms=?,
                updated_at_utc_ms=?
                WHERE id=?""",
                (
                    merged_mask,
                    pending_since,
                    now,
                    plan_id,
                ),
            )

            if previous_mask == 0:
                self.activity(
                    "info",
                    "AUTO_TRIGGER_PENDING",
                    "An automatic backup is waiting to run.",
                    plan_id,
                    details={
                        "trigger_mask": merged_mask,
                        "pending_since_utc_ms": pending_since,
                    },
                    conn=conn,
                )

            return merged_mask

    def clear_pending_trigger(
        self,
        plan_id: str,
    ) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM BackupPlans WHERE id=?",
                (plan_id,),
            ).fetchone()

            if not row:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")

            conn.execute(
                """UPDATE BackupPlans SET
                pending_trigger_mask=0,
                pending_trigger_since_utc_ms=NULL,
                updated_at_utc_ms=?
                WHERE id=?""",
                (
                    utc_ms(),
                    plan_id,
                ),
            )

    def pending_plans(self) -> list[PlanDetails]:
        with self.db.read_connection() as conn:
            rows = conn.execute(
                self._plan_query(
                    """WHERE p.state='enabled'
                    AND p.pending_trigger_mask != 0
                    ORDER BY p.pending_trigger_since_utc_ms ASC"""
                )
            ).fetchall()

            return [self._details(row) for row in rows]

    def update_last_auto_backup(
        self,
        plan_id: str,
        completed_at_utc_ms: int | None = None,
    ) -> None:
        completed_at = (
            utc_ms()
            if completed_at_utc_ms is None
            else int(completed_at_utc_ms)
        )

        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM BackupPlans WHERE id=?",
                (plan_id,),
            ).fetchone()

            if not row:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")

            conn.execute(
                """UPDATE BackupPlans SET
                last_auto_backup_at_utc_ms=?,
                updated_at_utc_ms=?
                WHERE id=?""",
                (
                    completed_at,
                    completed_at,
                    plan_id,
                ),
            )

    def update_last_source_scan_fingerprint(
        self,
        plan_id: str,
        fingerprint: str,
    ) -> None:
        if not isinstance(fingerprint, str):
            raise AppError(
                "SOURCE_FINGERPRINT_INVALID",
                "The source fingerprint must be a string.",
            )

        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM BackupPlans WHERE id=?",
                (plan_id,),
            ).fetchone()

            if not row:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")

            conn.execute(
                """UPDATE BackupPlans SET
                last_source_scan_fingerprint=?,
                updated_at_utc_ms=?
                WHERE id=?""",
                (
                    fingerprint,
                    utc_ms(),
                    plan_id,
                ),
            )

    def find_relocated_source(
        self,
        plan: PlanDetails,
    ) -> SourceDescriptor:
        if not plan.source_identity_json:
            raise AppError(
                "SOURCE_IDENTITY_UNCONFIRMED",
                "The plan source is not confirmed.",
            )

        original = Path(plan.source_path)

        try:
            original.lstat()
            exists = True
        except FileNotFoundError:
            exists = False
        except OSError as exc:
            raise AppError("SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                           "Could not check the source location.", str(exc)) from exc
        if exists:
            self.assert_source_identity(plan)
            try:
                return PathPolicy.validate_source(str(original))
            except AppError as exc:
                if exc.code == "SOURCE_ACCESS_DENIED" and getattr(exc.__cause__, "winerror", None) in (32, 33):
                    raise AppError("SOURCE_LOCKED", "The source is temporarily busy.", str(exc)) from exc
                raise

        parent = original.parent
        if (
            not parent.is_dir()
            or parent.is_symlink()
            or getattr(parent, "is_junction", lambda: False)()
        ):
            raise AppError(
                "SOURCE_NOT_FOUND",
                "The original folder is unavailable.",
            )

        candidates: list[Path] = []
        try:
            with os.scandir(parent) as listing:
                for entry in listing:
                    if len(candidates) == 256:
                        raise AppError(
                            "SOURCE_IDENTITY_AMBIGUOUS",
                            "The original folder has more than 256 candidates.",
                        )
                    candidates.append(Path(entry.path))
        except OSError as exc:
            raise AppError(
                "SOURCE_NOT_FOUND",
                "Could not read the original folder.",
                str(exc),
            ) from exc

        matches: list[Path] = []

        for candidate in candidates:
            if (
                candidate.is_symlink()
                or getattr(candidate, "is_junction", lambda: False)()
            ):
                continue

            try:
                identity = confirmed_source_identity(
                    candidate,
                    plan.source_kind,
                )
            except AppError as exc:
                if exc.code in {
                    "SOURCE_IDENTITY_UNAVAILABLE",
                    "SOURCE_ACCESS_DENIED",
                    "SOURCE_LOCKED",
                }:
                    raise AppError(
                        "SOURCE_IDENTITY_AMBIGUOUS",
                        "Not all candidates could be checked.",
                    ) from exc
                continue

            if identity == plan.source_identity_json:
                matches.append(candidate)

        if len(matches) != 1:
            raise AppError(
                (
                    "SOURCE_NOT_FOUND"
                    if not matches
                    else "SOURCE_IDENTITY_AMBIGUOUS"
                ),
                "The same source was not found unambiguously in the original folder.",
            )

        descriptor = PathPolicy.validate_source(str(matches[0]))
        storage = Path(plan.storage_path)

        if PathPolicy.inside(descriptor.path, storage):
            raise AppError(
                "STORAGE_TOPOLOGY_INVALID",
                "The source is inside the storage.",
            )

        PathPolicy.validate_topology(descriptor, storage)

        if (
            confirmed_source_identity(
                descriptor.path,
                descriptor.kind.value,
            )
            != plan.source_identity_json
        ):
            raise AppError(
                "SOURCE_IDENTITY_MISMATCH",
                "The candidate changed during the check.",
            )

        return descriptor

    def resolve_source_for_backup(
        self,
        plan_id: str,
        cancelled: Callable[[], bool],
    ) -> SourceDescriptor:
        plan = self.plan_details(plan_id)

        if cancelled():
            raise CancelledError()

        descriptor = self.find_relocated_source(plan)

        if descriptor.path == Path(plan.source_path):
            return descriptor

        if cancelled():
            raise CancelledError()

        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT source_path,source_identity_json "
                "FROM BackupPlans WHERE id=?",
                (plan_id,),
            ).fetchone()

            if (
                row is None
                or row["source_path"] != plan.source_path
                or row["source_identity_json"]
                != plan.source_identity_json
            ):
                raise AppError(
                    "SOURCE_IDENTITY_MISMATCH",
                    "The plan changed during the search.",
                )

            if (
                confirmed_source_identity(
                    descriptor.path,
                    descriptor.kind.value,
                )
                != plan.source_identity_json
            ):
                raise AppError(
                    "SOURCE_IDENTITY_MISMATCH",
                    "The candidate changed during binding.",
                )

            conn.execute(
                "UPDATE BackupPlans SET source_path=?,"
                "last_source_scan_fingerprint='',"
                "updated_at_utc_ms=? WHERE id=?",
                (str(descriptor.path), utc_ms(), plan_id),
            )

            self.activity(
                "info",
                "SOURCE_RELOCATED",
                "Source rename confirmed.",
                plan_id,
                details={
                    "old_path": plan.source_path,
                    "new_path": str(descriptor.path),
                },
                conn=conn,
            )

            now = utc_ms()
            conn.execute(
                "UPDATE Issues SET state='resolved',"
                "resolved_at_utc_ms=?,last_seen_at_utc_ms=? "
                "WHERE dedupe_key IN (?,?) "
                "AND state IN ('active','dismissed')",
                (
                    now,
                    now,
                    f"SOURCE_NOT_FOUND:{plan_id}",
                    f"SOURCE_IDENTITY_AMBIGUOUS:{plan_id}",
                ),
            )

        return descriptor

    def refresh_replaced_file_identity(self, plan: PlanDetails, current_identity: str) -> bool:
        """Accepts a file that was saved with replacement (Word and others) without asking the user."""
        if plan.source_kind != SourceKind.FILE.value or not plan.source_identity_json:
            return False
        if not file_replaced_in_place(Path(plan.source_path), plan.source_identity_json):
            return False
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE BackupPlans SET source_identity_json=?,last_source_scan_fingerprint='',updated_at_utc_ms=? "
                "WHERE id=? AND source_path=? AND source_identity_json=?",
                (current_identity, utc_ms(), plan.id, plan.source_path, plan.source_identity_json))
            if cursor.rowcount != 1:
                return False
            self.activity("info", "SOURCE_IDENTITY_REFRESHED",
                          "The file was saved by replacement (as Word and other editors do); the source was confirmed automatically.",
                          plan.id, details={"source_path": plan.source_path}, conn=conn)
            now = utc_ms()
            conn.execute("UPDATE Issues SET state='resolved',resolved_at_utc_ms=?,last_seen_at_utc_ms=? "
                         "WHERE plan_id=? AND code IN ('SOURCE_IDENTITY_UNCONFIRMED','SOURCE_IDENTITY_MISMATCH') "
                         "AND state IN ('active','dismissed')", (now, now, plan.id))
        return True

    def assert_source_identity(self, plan: PlanDetails) -> None:
        if not plan.source_identity_json:
            raise AppError("SOURCE_IDENTITY_UNCONFIRMED", "The source of an older plan is not confirmed.")
        current = confirmed_source_identity(Path(plan.source_path), plan.source_kind)
        if current != plan.source_identity_json:
            if self.refresh_replaced_file_identity(plan, current):
                return
            raise AppError("SOURCE_IDENTITY_MISMATCH", "A different object is at the plan's path.")

    def confirm_source_identity(self, plan_id: str) -> None:
        plan = self.plan_details(plan_id)
        source = PathPolicy.validate_source(plan.source_path)
        if source.kind.value != plan.source_kind:
            raise AppError("SOURCE_KIND_MISMATCH", "The source type changed.")
        PathPolicy.validate_topology(source, Path(plan.storage_path))
        identity = confirmed_source_identity(source.path, source.kind.value)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT source_path FROM BackupPlans WHERE id=?", (plan_id,)).fetchone()
            if not row or row["source_path"] != plan.source_path:
                raise AppError("SOURCE_IDENTITY_MISMATCH", "The plan path changed during confirmation.")
            conn.execute("UPDATE BackupPlans SET source_identity_json=?,last_source_scan_fingerprint='',updated_at_utc_ms=? WHERE id=?",
                         (identity, utc_ms(), plan_id))
            self.activity("warning", "SOURCE_MANUALLY_CONFIRMED", "Source confirmed by the user.",
                          plan_id, details={"source_path": plan.source_path}, conn=conn)
            now = utc_ms()
            conn.execute("UPDATE Issues SET state='resolved',resolved_at_utc_ms=?,last_seen_at_utc_ms=? "
                         "WHERE plan_id=? AND code IN ('SOURCE_IDENTITY_UNCONFIRMED','SOURCE_IDENTITY_MISMATCH') AND state IN ('active','dismissed')",
                         (now,now,plan_id))

    def adopt_restored_source_identity(self, plan_id: str) -> None:
        plan = self.plan_details(plan_id)
        identity = confirmed_source_identity(Path(plan.source_path), plan.source_kind)
        with self.db.transaction() as conn:
            conn.execute("UPDATE BackupPlans SET source_identity_json=?,last_source_scan_fingerprint='',updated_at_utc_ms=? WHERE id=?",
                         (identity, utc_ms(), plan_id))
            self.activity("info", "SOURCE_IDENTITY_AFTER_RESTORE", "The original was replaced with a saved version.", plan_id, conn=conn)
    def plan_deletion_job(self, plan_id: str) -> dict[str, Any] | None:
        with self.db.read_connection() as conn:
            row = conn.execute("SELECT * FROM PlanDeletionJobs WHERE plan_id=?", (plan_id,)).fetchone()
        return dict(row) if row else None

    def discard_plan_deletion_job(self, plan_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM PlanDeletionJobs WHERE plan_id=?", (plan_id,))

    def discard_plan_deletion_jobs(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM PlanDeletionJobs")

    def plan_deletion_preview(self, plan_id: str) -> PlanDeletionPreview:
        plan = self.plan_details(plan_id)
        with self.db.read_connection() as conn:
            rows = conn.execute(
                "SELECT id,state,sequence_no,pinned,stored_size_bytes FROM Snapshots WHERE plan_id=? ORDER BY sequence_no",
                (plan_id,),
            ).fetchall()
        versions = [row for row in rows if row["state"] != "deleted"]
        material = {"plan": plan.id, "name": plan.display_name, "source": plan.source_path,
                    "storage": plan.storage_path, "rows": [dict(row) for row in rows]}
        token = sha256_bytes(json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        return PlanDeletionPreview(plan.id, plan.display_name, plan.source_path, plan.storage_path,
                                   len(versions), sum(row["stored_size_bytes"] for row in versions),
                                   sum(bool(row["pinned"]) for row in versions), token)

    def begin_plan_deletion(self, plan_id: str, operation_id: str, inventory: dict[str, Any],
                            preview: PlanDeletionPreview) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT state FROM BackupPlans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")
            existing = conn.execute("SELECT 1 FROM PlanDeletionJobs WHERE plan_id=?", (plan_id,)).fetchone()
            if existing:
                raise AppError("PLAN_DELETE_CHANGED", "The deletion set changed.")
            current_plan = conn.execute(
                "SELECT p.display_name,p.source_path,s.root_path FROM BackupPlans p "
                "JOIN StorageRoots s ON s.id=p.storage_root_id WHERE p.id=?", (plan_id,),
            ).fetchone()
            if current_plan is None:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")
            current_rows = conn.execute(
                "SELECT id,state,sequence_no,pinned,stored_size_bytes FROM Snapshots WHERE plan_id=? ORDER BY sequence_no",
                (plan_id,),
            ).fetchall()
            material = {"plan": plan_id, "name": current_plan["display_name"], "source": current_plan["source_path"],
                        "storage": current_plan["root_path"], "rows": [dict(value) for value in current_rows]}
            current_token = sha256_bytes(json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            if current_token != preview.token:
                raise AppError("PLAN_DELETE_CHANGED", "The set of versions or their pinning changed before deletion started.")
            op = conn.execute("SELECT type,state,plan_id FROM Operations WHERE id=?", (operation_id,)).fetchone()
            if not op or op["type"] != OperationType.DELETE_PLAN_DATA.value or op["state"] != "running" or op["plan_id"] != plan_id:
                raise AppError("OPERATION_PAYLOAD_INVALID", "Plan deletion operation not found.")
            conn.execute(
                "INSERT INTO PlanDeletionJobs(plan_id,operation_id,inventory_json,preview_json,created_at_utc_ms) VALUES (?,?,?,?,?)",
                (plan_id, operation_id, json.dumps(inventory, ensure_ascii=False),
                 json.dumps({key: value for key, value in vars(preview).items() if key != "resuming"}, ensure_ascii=False), utc_ms()),
            )
            conn.execute("UPDATE BackupPlans SET state='archived',pending_trigger_mask=0,pending_trigger_since_utc_ms=NULL,updated_at_utc_ms=? WHERE id=?",
                         (utc_ms(),plan_id))
            conn.execute("UPDATE Snapshots SET state='deleting' WHERE plan_id=? AND state!='deleted'", (plan_id,))
            self.activity("warning", "PLAN_DELETE_STARTED", "Irreversible deletion of the plan and its copies started.",
                          plan_id, operation_id, {"name": preview.display_name, "count": preview.snapshot_count,
                                                  "pinned": preview.pinned_count, "root": inventory["root"]}, conn)

    def finish_plan_deletion(self, plan_id: str, operation_id: str,
                             deleted_files: int | None = None,
                             failed_files: int | None = None) -> None:
        with self.db.transaction() as conn:
            plan = conn.execute(
                "SELECT p.display_name,p.source_path,s.root_path FROM BackupPlans p JOIN StorageRoots s ON s.id=p.storage_root_id WHERE p.id=?",
                (plan_id,),
            ).fetchone()
            job = conn.execute("SELECT operation_id FROM PlanDeletionJobs WHERE plan_id=?", (plan_id,)).fetchone()
            operation = conn.execute(
                "SELECT type,state,plan_id FROM Operations WHERE id=?", (operation_id,)
            ).fetchone()
            if not plan or not job or job["operation_id"] != operation_id:
                raise AppError("PLAN_DELETE_CHANGED", "The deletion log changed.")
            if (not operation or operation["type"] != OperationType.DELETE_PLAN_DATA.value
                    or operation["plan_id"] != plan_id
                    or operation["state"] not in {"running", "needs_recovery"}):
                raise AppError("PLAN_DELETE_CHANGED", "The deletion operation does not match the log.")
            now = utc_ms()
            conn.execute(
                "INSERT INTO RetiredPlans(plan_id,display_name,source_path,storage_path,deleted_at_utc_ms,operation_id) VALUES (?,?,?,?,?,?)",
                (plan_id,plan["display_name"],plan["source_path"],plan["root_path"],now,operation_id),
            )
            details = {"deleted_plan_id": plan_id, "display_name": plan["display_name"],
                       "source_path": plan["source_path"], "storage_path": plan["root_path"]}
            message = "The plan and its copies were deleted."
            if deleted_files is not None:
                failed = int(failed_files or 0)
                details["deleted_files"] = int(deleted_files)
                details["failed_files"] = failed
                message = f"Deleted {int(deleted_files)} files, failed {failed}."
            self.activity("warning", "PLAN_DELETE_SUCCEEDED", message,
                          plan_id, operation_id, details, conn)
            conn.execute("UPDATE Operations SET state='failed',error_code='PLAN_DELETE_SUPERSEDED',finished_at_utc_ms=? "
                         "WHERE plan_id=? AND state='needs_recovery' AND type=?",
                         (now,plan_id,OperationType.DELETE_PLAN_DATA.value))
            conn.execute("UPDATE Operations SET state='succeeded',error_code=NULL,error_message=NULL,finished_at_utc_ms=? WHERE id=?",
                         (now,operation_id))
            # Preserve operation and audit history while removing FK references to the retired plan.
            conn.execute("UPDATE Operations SET plan_id=NULL WHERE plan_id=?", (plan_id,))
            conn.execute("UPDATE ActivityLog SET plan_id=NULL WHERE plan_id=?", (plan_id,))
            conn.execute("DELETE FROM BackupPlans WHERE id=?", (plan_id,))
    def plan_details(self, plan_id: str) -> PlanDetails:
        with self.db.read_connection() as conn:
            row = conn.execute(self._plan_query("WHERE p.id=?"), (plan_id,)).fetchone()
            if not row:
                raise AppError("PLAN_NOT_FOUND", "Plan not found.")
            return self._details(row)

    def plan_summaries(self, archived: bool = False) -> list[PlanSummary]:
        operator = "=" if archived else "!="
        with self.db.read_connection() as conn:
            rows = conn.execute(
                self._plan_query(f"WHERE p.state {operator} 'archived' ORDER BY p.updated_at_utc_ms DESC")
            ).fetchall()
            return [self._summary(row) for row in rows]

    def snapshot(self, snapshot_id: str) -> SnapshotInfo:
        with self.db.read_connection() as conn:
            row = conn.execute(
                """SELECT x.*, p.display_name, p.source_path, p.source_kind,
                p.state AS plan_state, s.root_path AS storage_path
                FROM Snapshots x
                JOIN BackupPlans p ON p.id=x.plan_id
                JOIN StorageRoots s ON s.id=p.storage_root_id
                WHERE x.id=?""",
                (snapshot_id,),
            ).fetchone()
            if not row:
                raise AppError("SNAPSHOT_NOT_FOUND", "Version not found.")
            return self._snapshot(row)

    def snapshots(self, plan_id: str, include_deleted: bool = False) -> list[SnapshotInfo]:
        sql = "SELECT * FROM Snapshots WHERE plan_id=?"
        if not include_deleted:
            sql += " AND state!='deleted'"
        sql += " ORDER BY sequence_no DESC"
        with self.db.read_connection() as conn:
            return [self._snapshot(row) for row in conn.execute(sql, (plan_id,)).fetchall()]

    def available_snapshots(self, plan_id: str) -> list[SnapshotInfo]:
        """Deterministic decision engine for automatic backup.

    The coordinator does no file I/O, creates no worker and does not run
    SnapshotService. It only:
    - records and merges pending triggers;
    - checks the plan policy and state;
    - returns a decision that the controller later uses to queue a backup.
    """
        with self.db.read_connection() as conn:
            rows = conn.execute(
                """SELECT x.*, p.display_name, p.source_path, p.source_kind,
                          p.state AS plan_state, s.root_path AS storage_path
                   FROM Snapshots x
                   JOIN BackupPlans p ON p.id = x.plan_id
                   JOIN StorageRoots s ON s.id = p.storage_root_id
                   WHERE x.plan_id = ? AND x.state = 'committed'
                   ORDER BY x.sequence_no DESC""",
                (plan_id,),
            ).fetchall()
            return [self._snapshot(row) for row in rows]

    def create_operation(
        self,
        kind: OperationType,
        plan_id: str | None,
        snapshot_id: str | None,
        payload: dict[str, Any],
    ) -> str:
        operation_id = str(uuid.uuid4())
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO Operations
                (id,type,state,plan_id,snapshot_id,payload_json,error_code,error_message,technical_details,created_at_utc_ms,started_at_utc_ms,finished_at_utc_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation_id, kind.value, "queued", plan_id, snapshot_id,
                    json.dumps(payload, ensure_ascii=False), None, None, None, utc_ms(), None, None,
                ),
            )
        return operation_id

    def operation(self, operation_id: str) -> sqlite3.Row:
        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM Operations WHERE id=?",
                (operation_id,),
            ).fetchone()

            if not row:
                raise AppError(
                    "OPERATION_NOT_FOUND",
                    "Operation not found.",
                )

            return row

    def last_lock_failure_at(self, plan_id: str) -> int | None:
        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT state,error_code,finished_at_utc_ms FROM Operations WHERE plan_id=? AND type=? "
                "ORDER BY created_at_utc_ms DESC,rowid DESC LIMIT 1",
                (plan_id, OperationType.BACKUP.value),
            ).fetchone()
        if row and row["state"] == "failed" and row["error_code"] == "SOURCE_LOCKED":
            return row["finished_at_utc_ms"]
        return None
    def last_backup_failed_from_lock(self, plan_id: str) -> bool:
        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT state,error_code FROM Operations WHERE plan_id=? AND type=? "
                "ORDER BY created_at_utc_ms DESC,rowid DESC LIMIT 1",
                (plan_id, OperationType.BACKUP.value),
            ).fetchone()
        return bool(row and row["state"] == "failed" and row["error_code"] == "SOURCE_LOCKED")
    def operation_payload(self, operation_id: str) -> dict[str, Any]:
        row = self.operation(operation_id)

        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise AppError(
                "OPERATION_PAYLOAD_INVALID",
                "The operation payload is corrupted.",
                str(exc),
            ) from exc

        if not isinstance(payload, dict):
            raise AppError(
                "OPERATION_PAYLOAD_INVALID",
                "The operation payload has an invalid structure.",
            )

        return payload

    def recovery_checkpoint(self, operation_id: str, details: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT state,payload_json FROM Operations WHERE id=?", (operation_id,)).fetchone()
            if row is None or row["state"] != "running":
                raise AppError("OPERATION_PAYLOAD_INVALID", "The operation is not running.")
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                raise AppError("OPERATION_PAYLOAD_INVALID", "The operation log is corrupted.")
            payload["recovery_checkpoint"] = details
            conn.execute("UPDATE Operations SET payload_json=? WHERE id=? AND state='running'",
                         (json.dumps(payload, ensure_ascii=False), operation_id))
            self.activity("info", "RESTORE_REPLACE_CHECKPOINT", "Replacement preparation recorded.",
                          operation_id=operation_id, details={"stage": "ready_to_publish"}, conn=conn)
    def operation_state(self, operation_id: str, state: str, error: AppError | None = None) -> None:
        with self.db.transaction() as conn:
            if state == "running":
                conn.execute(
                    "UPDATE Operations SET state=?, started_at_utc_ms=? WHERE id=?",
                    (state, utc_ms(), operation_id),
                )
            else:
                conn.execute(
                    """UPDATE Operations SET state=?, error_code=?, error_message=?,
                    technical_details=?, finished_at_utc_ms=? WHERE id=?""",
                    (
                        state,
                        error.code if error else None,
                        error.user_message if error else None,
                        error.technical_details if error else None,
                        utc_ms(),
                        operation_id,
                    ),
                )

    def operation_plan_id(self, operation_id: str) -> str | None:
        with self.db.read_connection() as conn:
            row = conn.execute("SELECT plan_id FROM Operations WHERE id=?", (operation_id,)).fetchone()
            return row["plan_id"] if row else None

    def create_snapshot_creating(self, plan_id: str, operation_id: str) -> tuple[str, int]:
        snapshot_id = str(uuid.uuid4())
        plan = self.plan_details(plan_id)

        if plan.storage_mode not in {mode.value for mode in StorageMode}:
            raise AppError(
                "STORAGE_MODE_INVALID",
                f"The plan has an unsupported storage mode: {plan.storage_mode}",
            )

        with self.db.transaction() as conn:
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence_no),0)+1 AS n "
                    "FROM Snapshots WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()["n"]
            )
            conn.execute(
                """INSERT INTO Snapshots
                (id,plan_id,operation_id,sequence_no,state,storage_mode,created_at_utc_ms,
                 artifact_relpath,manifest_relpath)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    plan_id,
                    operation_id,
                    sequence,
                    "creating",
                    plan.storage_mode,
                    utc_ms(),
                    "data",
                    "manifest.json",
                ),
            )
        return snapshot_id, sequence

    def commit_snapshot(
        self,
        snapshot_id: str,
        plan_id: str,
        operation_id: str,
        logical: int,
        stored: int,
        count: int,
        fingerprint: str,
        manifest_hash: str,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE Snapshots SET state='committed', logical_size_bytes=?, stored_size_bytes=?,
                file_count=?, source_fingerprint=?, manifest_sha256=? WHERE id=?""",
                (logical, stored, count, fingerprint, manifest_hash, snapshot_id),
            )
            conn.execute(
                "UPDATE BackupPlans SET last_success_snapshot_id=?, updated_at_utc_ms=? WHERE id=?",
                (snapshot_id, utc_ms(), plan_id),
            )
            conn.execute(
                "UPDATE Operations SET state='succeeded', finished_at_utc_ms=? WHERE id=?",
                (utc_ms(), operation_id),
            )
            self.activity(
                "info", "SNAPSHOT_COMMITTED", "A new saved version was created.",
                plan_id, operation_id, {"snapshot_id": snapshot_id}, conn,
            )

    def finalize_recovery(
        self,
        plan_id: str,
        review_operation_id: str,
        decisions: dict[str, dict[str, Any] | None],
        operation_reviews: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[int, int]:
        with self.db.transaction() as conn:
            review = conn.execute(
                "SELECT type, state, plan_id FROM Operations WHERE id=?",
                (review_operation_id,),
            ).fetchone()
            if (review is None or review["type"] != OperationType.RECOVER_PLAN.value
                    or review["state"] != "running" or review["plan_id"] != plan_id):
                raise AppError("OPERATION_PAYLOAD_INVALID", "The verification operation was not found or has already finished.")
            rows = conn.execute(
                "SELECT id, operation_id FROM Snapshots WHERE plan_id=? AND state='needs_recovery'",
                (plan_id,),
            ).fetchall()
            if {row["id"] for row in rows} != set(decisions):
                raise AppError("OPERATION_NEEDS_RECOVERY", "The set of unfinished versions changed. Repeat the check.")
            originals = conn.execute(
                "SELECT id,type FROM Operations WHERE plan_id=? AND state='needs_recovery'",
                (plan_id,),
            ).fetchall()
            if not rows and not originals:
                raise AppError("RECOVERY_NOT_REQUIRED", "Nothing to check.")
            accepted = 0
            unavailable = 0
            now = utc_ms()
            accepted_operations: set[str] = set()
            for row in rows:
                metadata = decisions[row["id"]]
                if metadata is not None and metadata.get("deleted") is True:
                    conn.execute("UPDATE Snapshots SET state='deleted', pinned=0 WHERE id=? AND state='needs_recovery'",
                                 (row["id"],))
                    self.activity("info", "SNAPSHOT_DELETE_RECOVERED", "Version deletion confirmed.",
                                  plan_id, review_operation_id, {"snapshot_id": row["id"]}, conn)
                elif metadata is None:
                    unavailable += 1
                    conn.execute(
                        "UPDATE Snapshots SET state='corrupted', verification_state='failed', verified_at_utc_ms=? "
                        "WHERE id=? AND plan_id=? AND state='needs_recovery'",
                        (now, row["id"], plan_id),
                    )
                    key = f"SNAPSHOT_CORRUPTED:{row['id']}"
                    conn.execute(
                        """INSERT INTO Issues
                        (id,dedupe_key,plan_id,severity,code,title,detail,state,
                         first_seen_at_utc_ms,last_seen_at_utc_ms,resolved_at_utc_ms)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(dedupe_key) DO UPDATE SET
                        severity=excluded.severity,title=excluded.title,detail=excluded.detail,
                        state='active',last_seen_at_utc_ms=excluded.last_seen_at_utc_ms,
                        resolved_at_utc_ms=NULL""",
                        (str(uuid.uuid4()), key, plan_id, IssueSeverity.CRITICAL.value,
                         "SNAPSHOT_CORRUPTED", "Version unusable",
                         "After a failure the version could not be fully verified. The files were kept, but the version is unavailable.",
                         "active", now, now, None),
                    )
                else:
                    accepted += 1
                    accepted_operations.add(row["operation_id"])
                    updated = conn.execute(
                        """UPDATE Snapshots SET state='committed', verification_state='passed',
                        verified_at_utc_ms=?, logical_size_bytes=?, stored_size_bytes=?,
                        file_count=?, source_fingerprint=?, manifest_sha256=?
                        WHERE id=? AND plan_id=? AND state='needs_recovery'""",
                        (now, metadata["logical_size_bytes"], metadata["stored_size_bytes"],
                         metadata["file_count"], metadata["source_fingerprint"],
                         metadata["manifest_sha256"], row["id"], plan_id),
                    )
                    if updated.rowcount != 1:
                        raise AppError("OPERATION_NEEDS_RECOVERY", "The version changed during verification.")
                    conn.execute(
                        "UPDATE Issues SET state='resolved', resolved_at_utc_ms=?, last_seen_at_utc_ms=? "
                        "WHERE dedupe_key=? AND state IN ('active','dismissed')",
                        (now, now, f"SNAPSHOT_CORRUPTED:{row['id']}"),
                    )
            for original in originals:
                review = (operation_reviews or {}).get(original["id"], {})
                state = review.get("state")
                if state not in {"succeeded", "failed"}:
                    state = "succeeded" if original["id"] in accepted_operations else "failed"
                code = None if state == "succeeded" else review.get("error_code", "RECOVERY_REVIEWED")
                conn.execute(
                    "UPDATE Operations SET state=?, error_code=?, finished_at_utc_ms=? "
                    "WHERE id=? AND state='needs_recovery'",
                    (state, code, now, original["id"]),
                )
                if review.get("manual_review"):
                    detail = review["detail"]
                    self.activity("warning", "RECOVERY_MANUAL_REVIEW", detail,
                                  plan_id, original["id"], conn=conn)
                    conn.execute(
                        """INSERT INTO Issues
                        (id,dedupe_key,plan_id,severity,code,title,detail,state,
                         first_seen_at_utc_ms,last_seen_at_utc_ms,resolved_at_utc_ms)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO UPDATE SET
                        detail=excluded.detail,state='active',
                        last_seen_at_utc_ms=excluded.last_seen_at_utc_ms,resolved_at_utc_ms=NULL""",
                        (str(uuid.uuid4()), f"RESTORE_RECOVERY_REVIEW_REQUIRED:{plan_id}", plan_id,
                         IssueSeverity.CRITICAL.value, "RESTORE_RECOVERY_REVIEW_REQUIRED",
                         "Check the restored file", detail, "active", now, now, None),
                    )
                elif state == "succeeded" and original["type"] == OperationType.RESTORE_REPLACE_FILE.value:
                    plan_row = conn.execute("SELECT source_path,source_kind FROM BackupPlans WHERE id=?", (plan_id,)).fetchone()
                    identity = confirmed_source_identity(Path(plan_row["source_path"]), plan_row["source_kind"])
                    conn.execute("UPDATE BackupPlans SET source_identity_json=?,last_source_scan_fingerprint='' WHERE id=?",
                                 (identity, plan_id))
                    self.activity("info", "RESTORE_REPLACE_RECONCILED",
                                  "File replacement verified after a failure.", plan_id, original["id"], conn=conn)
            latest = conn.execute(
                "SELECT id FROM Snapshots WHERE plan_id=? AND state='committed' "
                "ORDER BY sequence_no DESC LIMIT 1", (plan_id,),
            ).fetchone()
            conn.execute(
                "UPDATE BackupPlans SET last_success_snapshot_id=?, updated_at_utc_ms=? WHERE id=?",
                (latest["id"] if latest else None, now, plan_id),
            )
            conn.execute(
                "UPDATE Operations SET state='succeeded', finished_at_utc_ms=? "
                "WHERE id=? AND state='running'",
                (now, review_operation_id),
            )
            remaining = conn.execute(
                "SELECT 1 FROM Snapshots WHERE plan_id=? AND state='needs_recovery' LIMIT 1",
                (plan_id,),
            ).fetchone()
            if remaining is None:
                conn.execute(
                    "UPDATE Issues SET state='resolved',resolved_at_utc_ms=?,last_seen_at_utc_ms=? "
                    "WHERE dedupe_key=? AND state IN ('active','dismissed')",
                    (now, now, f"OPERATION_NEEDS_RECOVERY:{plan_id}"),
                )
            self.activity(
                "warning" if unavailable else "info", "RECOVERY_REVIEWED",
                "Check of the interrupted operation finished. Leftover data was not deleted.",
                plan_id, review_operation_id,
                {"accepted": accepted, "unavailable": unavailable}, conn,
            )
            return accepted, unavailable

    def mark_snapshot(self, snapshot_id: str, state: str) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT plan_id,state FROM Snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if row is None:
                raise AppError("SNAPSHOT_NOT_FOUND", "Version not found.")
            if state == "deleting" and row["state"] == "committed":
                self.activity("warning", "SNAPSHOT_DELETE_STARTED", "Version deletion started.",
                              row["plan_id"], details={"snapshot_id": snapshot_id}, conn=conn)
            conn.execute("UPDATE Snapshots SET state=? WHERE id=?", (state, snapshot_id))

    def mark_delete_needs_recovery(
        self,
        snapshot_id: str,
        detail: str,
    ) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT plan_id, state FROM Snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if row is None or row["state"] not in {
                "deleting", "needs_recovery"
            }:
                raise AppError(
                    "SNAPSHOT_NOT_COMMITTED",
                    "This version cannot be marked for a post-deletion check.",
                )

            conn.execute(
                "UPDATE Snapshots SET state='needs_recovery' WHERE id=?",
                (snapshot_id,),
            )
            self.activity(
                "warning",
                "OPERATION_NEEDS_RECOVERY",
                detail,
                row["plan_id"],
                details={"snapshot_id": snapshot_id},
                conn=conn,
            )

    def quarantine_unfinished_snapshot(
        self,
        snapshot_id: str,
        detail: str,
    ) -> str | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT plan_id, state FROM Snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                return None

            if row["state"] not in {
                "creating", "deleting", "needs_recovery"
            }:
                return None

            if row["state"] != "needs_recovery":
                conn.execute(
                    "UPDATE Snapshots SET state='needs_recovery' WHERE id=?",
                    (snapshot_id,),
                )
                self.activity(
                    "warning",
                    "OPERATION_NEEDS_RECOVERY",
                    detail,
                    row["plan_id"],
                    details={"snapshot_id": snapshot_id},
                    conn=conn,
                )

            return str(row["plan_id"])

    def mark_snapshot_corrupted(self, snapshot_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE Snapshots SET state='corrupted', verification_state='failed', verified_at_utc_ms=? WHERE id=?",
                (utc_ms(), snapshot_id),
            )

    def last_fingerprint(self, plan_id: str) -> str:
        with self.db.read_connection() as conn:
            row = conn.execute(
                """SELECT source_fingerprint FROM Snapshots
                WHERE plan_id=? AND state='committed' ORDER BY sequence_no DESC LIMIT 1""",
                (plan_id,),
            ).fetchone()
            return row["source_fingerprint"] if row else ""

    def update_verification(self, snapshot_id: str, passed: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE Snapshots SET state=?, verification_state=?, verified_at_utc_ms=? WHERE id=?",
                ("committed" if passed else "corrupted", "passed" if passed else "failed", utc_ms(), snapshot_id),
            )

    def set_pin(self, snapshot_id: str, pinned: bool) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT plan_id FROM Snapshots WHERE id=? AND state='committed'",
                (snapshot_id,),
            ).fetchone()
            if not row:
                raise AppError("SNAPSHOT_NOT_COMMITTED", "Only an available saved version can be pinned.")
            conn.execute("UPDATE Snapshots SET pinned=? WHERE id=?", (1 if pinned else 0, snapshot_id))
            self.activity(
                "info",
                "SNAPSHOT_PINNED" if pinned else "SNAPSHOT_UNPINNED",
                "Version pinned." if pinned else "Version unpinned.",
                row["plan_id"],
                conn=conn,
            )

    def delete_record(self, snapshot_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE Snapshots SET state='deleted' WHERE id=?", (snapshot_id,))

    def archive(self, plan_id: str, operation_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE BackupPlans SET state='archived', updated_at_utc_ms=? WHERE id=?",
                (utc_ms(), plan_id),
            )
            conn.execute(
                "UPDATE Operations SET state='succeeded', finished_at_utc_ms=? WHERE id=?",
                (utc_ms(), operation_id),
            )
            self.activity(
                "info", "PLAN_ARCHIVED", "Plan archived. Saved versions remain available.",
                plan_id, operation_id, conn=conn,
            )

    def runtime_status_counts(self) -> dict[str, int]:
        with self.db.read_connection() as conn:
            active_errors = conn.execute(
                """SELECT COUNT(*) FROM Issues
                WHERE state='active'
                AND severity IN ('error','critical')"""
            ).fetchone()[0]

            active_warnings = conn.execute(
                """SELECT COUNT(*) FROM Issues
                WHERE state='active'
                AND severity='warning'"""
            ).fetchone()[0]

            enabled_plans = conn.execute(
                """SELECT COUNT(*) FROM BackupPlans
                WHERE state='enabled'"""
            ).fetchone()[0]

            pending_plans = conn.execute(
                """SELECT COUNT(*) FROM BackupPlans
                WHERE state='enabled'
                AND pending_trigger_mask != 0"""
            ).fetchone()[0]

            return {
                "active_errors": int(active_errors),
                "active_warnings": int(active_warnings),
                "enabled_plans": int(enabled_plans),
                "pending_plans": int(pending_plans),
            }

    def recent_activity(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.db.read_connection() as conn:
            return list(
                conn.execute(
                    """SELECT a.*, p.display_name FROM ActivityLog a
                    LEFT JOIN BackupPlans p ON p.id=a.plan_id
                    ORDER BY a.created_at_utc_ms DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            )

    def unfinished_operations(self) -> list[sqlite3.Row]:
        with self.db.read_connection() as conn:
            return list(
                conn.execute("SELECT * FROM Operations WHERE state IN ('queued','running')").fetchall()
            )

    def unfinished_snapshots(self) -> list[SnapshotInfo]:
        with self.db.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM Snapshots "
                "WHERE state IN ('creating', 'deleting', 'needs_recovery') "
                "ORDER BY created_at_utc_ms, id"
            ).fetchall()
            return [self._snapshot(row) for row in rows]

    def unfinished_operations_for_plan(self, plan_id: str) -> list[sqlite3.Row]:
        with self.db.read_connection() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM Operations WHERE plan_id=? AND state IN ('queued','running')",
                    (plan_id,),
                ).fetchall()
            )

    def needs_recovery_for_plan(self, plan_id: str) -> bool:
        with self.db.read_connection() as conn:
            operation = conn.execute(
                "SELECT 1 FROM Operations WHERE plan_id=? AND state='needs_recovery' LIMIT 1",
                (plan_id,),
            ).fetchone()
            snapshot = conn.execute(
                "SELECT 1 FROM Snapshots WHERE plan_id=? AND state='needs_recovery' LIMIT 1",
                (plan_id,),
            ).fetchone()
            deletion = conn.execute("SELECT 1 FROM PlanDeletionJobs WHERE plan_id=?", (plan_id,)).fetchone()
            return bool(operation or snapshot or deletion)

    def recovery_mark(self, operation_id: str) -> tuple[str | None, str | None]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT plan_id, snapshot_id FROM Operations WHERE id=?", (operation_id,)
            ).fetchone()
            if not row:
                return None, None
            conn.execute(
                "UPDATE Operations SET state='needs_recovery', finished_at_utc_ms=? WHERE id=?",
                (utc_ms(), operation_id),
            )
            if row["snapshot_id"]:
                conn.execute(
                    "UPDATE Snapshots SET state='needs_recovery' WHERE id=? AND state='creating'",
                    (row["snapshot_id"],),
                )
            conn.execute(
                "UPDATE Snapshots SET state='needs_recovery' WHERE operation_id=? AND state='creating'",
                (operation_id,),
            )
            self.activity(
                "warning", "OPERATION_NEEDS_RECOVERY",
                "The previous operation was interrupted and needs a check.",
                row["plan_id"], operation_id, conn=conn,
            )
            return row["plan_id"], row["snapshot_id"]

    def upsert_issue(self, draft: IssueDraft) -> None:
        now = utc_ms()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id, state FROM Issues WHERE dedupe_key=?",
                (draft.dedupe_key,),
            ).fetchone()

            if row:
                next_state = (
                    "dismissed"
                    if row["state"] == "dismissed"
                    else "active"
                )
                conn.execute(
                    """UPDATE Issues SET severity=?, title=?, detail=?,
                    state=?, last_seen_at_utc_ms=?, resolved_at_utc_ms=NULL
                    WHERE id=?""",
                    (
                        draft.severity,
                        draft.title,
                        draft.detail,
                        next_state,
                        now,
                        row["id"],
                    ),
                )
                return

            conn.execute(
                """INSERT INTO Issues
                (id,dedupe_key,plan_id,severity,code,title,detail,state,
                 first_seen_at_utc_ms,last_seen_at_utc_ms,resolved_at_utc_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    draft.dedupe_key,
                    draft.plan_id,
                    draft.severity,
                    draft.code,
                    draft.title,
                    draft.detail,
                    "active",
                    now,
                    now,
                    None,
                ),
            )

    def resolve_issue(self, dedupe_key: str) -> None:
        now = utc_ms()
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE Issues SET state='resolved',
                resolved_at_utc_ms=?, last_seen_at_utc_ms=?
                WHERE dedupe_key=? AND state IN ('active','dismissed')""",
                (now, now, dedupe_key),
            )

    def dismiss_issue(self, dedupe_key: str) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                """SELECT id, plan_id, title FROM Issues
                WHERE dedupe_key=? AND state='active'""",
                (dedupe_key,),
            ).fetchone()

            if not row:
                raise AppError(
                    "ISSUE_NOT_ACTIVE",
                    "The issue is already hidden, closed or not found.",
                )

            now = utc_ms()
            conn.execute(
                """UPDATE Issues SET state='dismissed',
                last_seen_at_utc_ms=?, resolved_at_utc_ms=NULL
                WHERE id=?""",
                (now, row["id"]),
            )
            self.activity(
                "info",
                "ISSUE_DISMISSED",
                f"Issue card hidden: {row['title']}",
                row["plan_id"],
                details={"dedupe_key": dedupe_key},
                conn=conn,
            )

    def resolve_plan_issue(self, plan_id: str, code: str) -> None:
        self.resolve_issue(f"{code}:{plan_id}")

    def list_active_issues(self, plan_id: str | None = None) -> list[HealthIssue]:
        query = "SELECT * FROM Issues WHERE state='active'"
        params: tuple[Any, ...] = ()
        if plan_id is not None:
            query += " AND (plan_id=? OR plan_id IS NULL)"
            params = (plan_id,)
        query += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 ELSE 2 END, last_seen_at_utc_ms DESC"
        with self.db.read_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                HealthIssue(
                    row["dedupe_key"], row["plan_id"], row["severity"], row["code"],
                    row["title"], row["detail"], row["first_seen_at_utc_ms"],
                    row["last_seen_at_utc_ms"],
                )
                for row in rows
            ]


class StorageService:
    ZIP_ARTIFACT_NAME = "artifact.zip"
    MANIFEST_NAME = "manifest.json"
    SNAPSHOT_METADATA_NAME = "snapshot.json"
    DIRECT_DATA_DIR_NAME = "data"

    def ensure_root(self, root: Path, storage_id: str) -> None:
        managed = root / "_amvibackup"
        (managed / "temporary").mkdir(parents=True, exist_ok=True)

        marker = managed / "storage.json"
        if not marker.exists():
            marker.write_text(
                json_text(
                    {
                        "application": APP_NAME,
                        "format_version": 1,
                        "storage_id": storage_id,
                        "created_at_utc": utc_iso(),
                        "storage_root_path": str(root),
                    }
                ),
                encoding="utf-8",
            )

    def plan_dir(self, storage_root: Path, plan: PlanDetails) -> Path:
        default = storage_root / f"{sanitize_name(plan.display_name)}__job-{plan.id[:8]}"
        if not storage_root.exists():
            return default
        suffix = f"__job-{plan.id[:8]}".casefold()
        matches: list[Path] = []
        try:
            for candidate in storage_root.iterdir():
                if not candidate.name.casefold().endswith(suffix):
                    continue
                if not candidate.is_dir() or candidate.is_symlink() or not PathPolicy.inside(candidate, storage_root):
                    raise AppError("SNAPSHOT_PATH_INVALID", "The plan folder has an unsafe path.")
                marker = candidate / "plan.json"
                if marker.is_file() and not marker.is_symlink():
                    identity = json.loads(marker.read_text(encoding="utf-8"))
                    valid = isinstance(identity, dict) and identity.get("plan_id") == plan.id
                else:
                    valid = False
                    snapshots_dir = candidate / "snapshots"
                    if snapshots_dir.is_dir() and not snapshots_dir.is_symlink():
                        for version in snapshots_dir.iterdir():
                            if not version.is_dir() or version.is_symlink() or not PathPolicy.inside(version, snapshots_dir):
                                raise AppError("SNAPSHOT_PATH_INVALID", "The version folder has an unsafe path.")
                            metadata = version / self.SNAPSHOT_METADATA_NAME
                            if metadata.is_file() and not metadata.is_symlink():
                                identity = json.loads(metadata.read_text(encoding="utf-8"))
                                if isinstance(identity, dict) and identity.get("plan_id") == plan.id:
                                    valid = True
                                    break
                if not valid:
                    raise AppError("SNAPSHOT_PATH_INVALID", "Could not confirm that the folder belongs to the plan.")
                matches.append(candidate)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AppError("SNAPSHOT_PATH_INVALID", "Could not check the plan folder.", str(exc)) from exc
        if len(matches) > 1:
            raise AppError("SNAPSHOT_PATH_INVALID", "Several folders of the same plan were found.")
        return matches[0] if matches else default

    def ensure_plan_dir(self, storage_root: Path, plan: PlanDetails) -> Path:
        directory = self.plan_dir(storage_root, plan)
        (directory / "snapshots").mkdir(parents=True, exist_ok=True)

        marker = directory / "plan.json"
        if not marker.exists():
            marker.write_text(
                json_text(
                    {
                        "format_version": 1,
                        "plan_id": plan.id,
                        "display_name_at_creation": plan.display_name,
                        "source_kind": plan.source_kind,
                        "storage_mode": plan.storage_mode,
                        "created_at_utc": utc_iso(),
                    }
                ),
                encoding="utf-8",
            )
        return directory

    def snapshot_dir(self, storage_root: Path, plan: PlanDetails, sequence_no: int) -> Path:
        return self.plan_dir(storage_root, plan) / "snapshots" / f"v{sequence_no:06d}"

    def artifact_paths(
        self,
        storage_root: Path,
        plan: PlanDetails,
        sequence_no: int,
    ) -> SnapshotArtifactPaths:
        snapshot_root = self.snapshot_dir(storage_root, plan, sequence_no)
        return SnapshotArtifactPaths(
            snapshot_root=snapshot_root,
            direct_data_root=snapshot_root / self.DIRECT_DATA_DIR_NAME,
            zip_artifact=snapshot_root / self.ZIP_ARTIFACT_NAME,
            manifest_path=snapshot_root / self.MANIFEST_NAME,
            snapshot_metadata_path=snapshot_root / self.SNAPSHOT_METADATA_NAME,
        )

    def temporary_operation_dir(self, storage_root: Path, operation_id: str) -> Path:
        return storage_root / "_amvibackup" / "temporary" / operation_id

    def temporary_snapshot_dir(
        self,
        storage_root: Path,
        operation_id: str,
        storage_mode: str,
    ) -> Path:
        base = self.temporary_operation_dir(storage_root, operation_id)
        if storage_mode == StorageMode.DIRECT.value:
            return base
        if storage_mode == StorageMode.ZIP.value:
            return base / "zip-snapshot"
        raise AppError(
            "STORAGE_MODE_INVALID",
            f"Unsupported storage mode: {storage_mode}",
        )

    def safe_snapshot_root(
        self,
        storage_root: Path,
        plan: PlanDetails,
        sequence_no: int,
    ) -> Path:
        candidate = self.snapshot_dir(storage_root, plan, sequence_no)
        plan_root = self.plan_dir(storage_root, plan)
        snapshots_root = plan_root / "snapshots"

        if not PathPolicy.inside(candidate, snapshots_root):
            raise AppError(
                "SNAPSHOT_PATH_INVALID",
                "The snapshot path points outside the managed storage.",
            )
        return candidate

    def finalize_committed_artifact(
        self,
        storage_root: Path,
        plan: PlanDetails,
        sequence_no: int,
        storage_mode: str,
    ) -> None:
        paths = self.artifact_paths(
            storage_root,
            plan,
            sequence_no,
        )

        if storage_mode == StorageMode.DIRECT.value:
            artifact = paths.direct_data_root
        elif storage_mode == StorageMode.ZIP.value:
            artifact = paths.zip_artifact
        else:
            raise AppError(
                "STORAGE_MODE_INVALID",
                f"Unsupported storage mode: {storage_mode}",
            )

        if not artifact.exists():
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The version artifact is missing after publishing.",
            )

        set_read_only_best_effort(artifact)
        set_read_only_best_effort(paths.manifest_path)
        set_read_only_best_effort(paths.snapshot_metadata_path)


    def prepare_snapshot_for_deletion(
        self,
        storage_root: Path,
        plan: PlanDetails,
        sequence_no: int,
    ) -> Path:
        snapshot_root = self.safe_snapshot_root(
            storage_root,
            plan,
            sequence_no,
        )
        set_writable_best_effort(snapshot_root)
        return snapshot_root


class Scanner:
    @staticmethod
    def scan(
        source: SourceDescriptor,
        cancelled: Callable[[], bool],
        progress: Callable[[ProgressEvent], None],
        operation_id: str,
    ) -> tuple[list[dict[str, Any]], int, str]:
        entries: list[dict[str, Any]] = []
        total = 0
        if source.kind == SourceKind.FILE:
            try:
                stat = source.path.stat()
            except FileNotFoundError as exc:
                raise AppError("SOURCE_CHANGED_DURING_READ", "The source disappeared during scanning.", str(exc)) from exc
            except OSError as exc:
                raise AppError("SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                               "Could not read the source.", str(exc)) from exc
            entries.append(
                {
                    "relative_path": source.path.name,
                    "entry_type": "file",
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "source_path": source.path,
                    "scan_device": stat.st_dev,
                    "scan_file_id": stat.st_ino,
                }
            )
            total = stat.st_size
        else:
            def walk_error(exc: OSError) -> None:
                raise AppError(
                    "SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                    "Could not walk through the source folder.", str(exc),
                ) from exc

            for directory, dirs, files in os.walk(source.path, followlinks=False, onerror=walk_error):
                if cancelled():
                    raise CancelledError()
                root = Path(directory)
                dirs[:] = [name for name in dirs
                           if not (root / name).is_symlink()
                           and not getattr(root / name, "is_junction", lambda: False)()]
                for name in dirs:
                    path = root / name
                    try:
                        stat = path.stat()
                    except OSError as exc:
                        raise AppError(
                            "SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                            "Could not check a subfolder of the source.", str(exc),
                        ) from exc
                    entries.append(
                        {
                            "relative_path": path.relative_to(source.path).as_posix(),
                            "entry_type": "directory",
                            "size_bytes": 0,
                            "mtime_ns": stat.st_mtime_ns,
                            "source_path": path,
                        }
                    )
                for name in files:
                    if cancelled():
                        raise CancelledError()
                    path = root / name
                    if path.is_symlink():
                        continue
                    try:
                        stat = path.stat()
                    except FileNotFoundError as exc:
                        raise AppError(
                            "SOURCE_CHANGED_DURING_READ",
                            "The source changed during scanning.",
                            str(exc),
                        ) from exc
                    except OSError as exc:
                        raise AppError("SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                                       "Could not check a source file.", str(exc)) from exc
                    entries.append(
                        {
                            "relative_path": path.relative_to(source.path).as_posix(),
                            "entry_type": "file",
                            "size_bytes": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                            "source_path": path,
                            "scan_device": stat.st_dev,
                            "scan_file_id": stat.st_ino,
                        }
                    )
                    total += stat.st_size
                progress(
                    ProgressEvent(
                        operation_id, "Preparing the file list…", str(root), len(entries), 0, total, 0
                    )
                )
        material = [
            {key: item[key] for key in ("relative_path", "entry_type", "size_bytes", "mtime_ns")}
            for item in entries
        ]
        material.sort(key=lambda item: item["relative_path"].casefold())
        fingerprint = sha256_bytes(
            json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        return entries, total, fingerprint


class ZipArtifactWriter:
    FORMAT_VERSION = 1

    @staticmethod
    def _zip_member_name(relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/").strip("/")
        if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
            raise AppError(
                "ZIP_ENTRY_INVALID",
                "Cannot write an unsafe path into the ZIP artifact.",
            )
        return normalized

    def write(
        self,
        artifact_path: Path,
        source_kind: str,
        entries: list[dict[str, Any]],
        cancelled: Callable[[], bool],
        progress: Callable[[ProgressEvent], None],
        operation_id: str,
        total_bytes: int,
    ) -> tuple[list[dict[str, Any]], int, int]:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)

        manifest_entries: list[dict[str, Any]] = []
        file_entries = [item for item in entries if item["entry_type"] == "file"]
        completed_files = 0
        completed_bytes = 0

        try:
            with zipfile.ZipFile(
                artifact_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=True,
            ) as archive:
                for entry in entries:
                    if cancelled():
                        raise CancelledError()

                    relative_path = self._zip_member_name(str(entry["relative_path"]))

                    if entry["entry_type"] == "directory":
                        directory_name = relative_path.rstrip("/") + "/"
                        info = zipfile.ZipInfo(directory_name)
                        info.external_attr = 0o40755 << 16
                        archive.writestr(info, b"")
                        manifest_entries.append(
                            {
                                "relative_path": relative_path,
                                "entry_type": "directory",
                                "size_bytes": 0,
                                "mtime_ns": entry["mtime_ns"],
                                "sha256": None,
                            }
                        )
                        continue

                    digest = hashlib.sha256()
                    info = zipfile.ZipInfo(relative_path)
                    try:
                        timestamp = datetime.fromtimestamp(entry["source_path"].stat().st_mtime)
                    except FileNotFoundError as exc:
                        raise AppError("SOURCE_CHANGED_DURING_READ", "The file disappeared before it was written to the ZIP.", str(exc)) from exc
                    except OSError as exc:
                        raise AppError("SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                                       "Could not read the source properties.", str(exc)) from exc
                    info.date_time = (
                        max(1980, timestamp.year),
                        timestamp.month,
                        timestamp.day,
                        timestamp.hour,
                        timestamp.minute,
                        timestamp.second,
                    )
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16

                    try:
                        try:
                            stream = entry["source_path"].open("rb")
                        except FileNotFoundError:
                            raise
                        except OSError as exc:
                            raise AppError("SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                                           "Could not open the source file.", str(exc)) from exc
                        with stream as source, archive.open(info, "w") as destination:
                            assert_scanned_source_handle(source, entry)
                            copied_bytes = 0
                            while True:
                                if cancelled():
                                    raise CancelledError()
                                try:
                                    chunk = source.read(COPY_CHUNK_SIZE)
                                except OSError as exc:
                                    raise AppError("SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                                                   "Could not read the source file.", str(exc)) from exc
                                if not chunk:
                                    break
                                destination.write(chunk)
                                digest.update(chunk)
                                completed_bytes += len(chunk)
                                copied_bytes += len(chunk)
                                progress(
                                    ProgressEvent(
                                        operation_id,
                                        "Creating the ZIP version…",
                                        str(entry["source_path"]),
                                        completed_files,
                                        len(file_entries),
                                        completed_bytes,
                                        total_bytes,
                                    )
                                )
                            assert_scanned_source_handle(source, entry)
                            if copied_bytes != int(entry["size_bytes"]):
                                raise AppError("SOURCE_CHANGED_DURING_READ", "The size of the file read for the ZIP changed.")
                    except FileNotFoundError as exc:
                        raise AppError(
                            "SOURCE_CHANGED_DURING_READ",
                            "The source file disappeared while the ZIP version was being created.",
                            str(exc),
                        ) from exc

                    completed_files += 1
                    manifest_entries.append(
                        {
                            "relative_path": relative_path,
                            "entry_type": "file",
                            "size_bytes": int(entry["size_bytes"]),
                            "mtime_ns": int(entry["mtime_ns"]),
                            "sha256": digest.hexdigest(),
                        }
                    )
        except zipfile.BadZipFile as exc:
            raise AppError(
                "ZIP_WRITE_FAILED",
                "Could not create the ZIP artifact.",
                str(exc),
            ) from exc
        except OSError as exc:
            raise AppError(
                "ZIP_WRITE_FAILED",
                "Could not write the ZIP artifact to the storage.",
                str(exc),
            ) from exc

        manifest_entries.sort(key=lambda item: item["relative_path"].casefold())

        try:
            stored_size = artifact_path.stat().st_size
        except OSError as exc:
            raise AppError(
                "ZIP_WRITE_FAILED",
                "The ZIP artifact was not found after writing finished.",
                str(exc),
            ) from exc

        return manifest_entries, stored_size, len(file_entries)


class ZipArtifactReader:
    @staticmethod
    def _safe_member_name(member_name: str) -> str:
        normalized = member_name.replace("\\", "/")

        if (
            not normalized
            or normalized.startswith("/")
            or normalized.startswith("\\")
            or ":" in normalized.split("/")[0]
            or ".." in normalized.split("/")
        ):
            raise AppError(
                "ZIP_TRAVERSAL_BLOCKED",
                f"The ZIP contains an unsafe path: {member_name}",
            )

        return normalized

    @staticmethod
    def _manifest_files(
        manifest_entries: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        files: dict[str, dict[str, Any]] = {}

        for entry in manifest_entries:
            if entry.get("entry_type") != "file":
                continue

            relative_path = ZipArtifactReader._safe_member_name(
                str(entry.get("relative_path", ""))
            )
            if relative_path in files:
                raise AppError(
                    "MANIFEST_INVALID",
                    f"The manifest contains a duplicate file: "
                    f"{relative_path}",
                )

            size = entry.get("size_bytes")
            digest = entry.get("sha256")

            if (
                type(size) is not int
                or size < 0
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in digest
                )
            ):
                raise AppError(
                    "MANIFEST_INVALID",
                    f"The manifest contains an invalid file entry: "
                    f"{relative_path}",
                )

            files[relative_path] = entry

        return files

    @staticmethod
    def _manifest_directories(
        manifest_entries: list[dict[str, Any]],
    ) -> set[str]:
        directories: set[str] = set()

        for entry in manifest_entries:
            if entry.get("entry_type") != "directory":
                continue

            relative_path = ZipArtifactReader._safe_member_name(
                str(entry.get("relative_path", ""))
            ).rstrip("/")

            if not relative_path:
                raise AppError(
                    "MANIFEST_INVALID",
                    "The manifest contains an empty directory path.",
                )

            directories.add(relative_path)

        return directories

    def _archive_members(
        self,
        artifact_path: Path,
        expected_files: dict[str, dict[str, Any]],
        expected_directories: set[str],
        cancelled: Callable[[], bool],
    ) -> dict[str, zipfile.ZipInfo]:
        if not artifact_path.is_file():
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The ZIP artifact is missing from the storage.",
            )

        try:
            with zipfile.ZipFile(artifact_path, mode="r") as archive:
                members: dict[str, zipfile.ZipInfo] = {}

                for info in archive.infolist():
                    if cancelled():
                        raise CancelledError()

                    member_name = self._safe_member_name(
                        info.filename
                    )

                    if info.is_dir():
                        normalized_dir = member_name.rstrip("/")
                        if (
                            normalized_dir
                            and normalized_dir
                            not in expected_directories
                        ):
                            raise AppError(
                                "SNAPSHOT_CORRUPTED",
                                "The ZIP contains an unexpected "
                                f"directory: {member_name}",
                            )
                        continue

                    if member_name in members:
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            "The ZIP contains a duplicate entry: "
                            f"{member_name}",
                        )

                    members[member_name] = info

                if set(members) != set(expected_files):
                    missing = sorted(
                        set(expected_files) - set(members)
                    )
                    unexpected = sorted(
                        set(members) - set(expected_files)
                    )
                    details: list[str] = []

                    if missing:
                        details.append(
                            "missing: "
                            + ", ".join(missing[:3])
                        )
                    if unexpected:
                        details.append(
                            "extra: "
                            + ", ".join(unexpected[:3])
                        )

                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "The ZIP content does not match the manifest"
                        + (
                            f" ({'; '.join(details)})"
                            if details
                            else "."
                        ),
                    )

                return members

        except zipfile.BadZipFile as exc:
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The ZIP artifact is corrupted or is not a ZIP file.",
                str(exc),
            ) from exc

    def verify(
        self,
        artifact_path: Path,
        manifest_entries: list[dict[str, Any]],
        cancelled: Callable[[], bool],
        progress: Callable[[ProgressEvent], None],
        operation_id: str,
    ) -> None:
        expected_files = self._manifest_files(manifest_entries)
        expected_directories = self._manifest_directories(
            manifest_entries
        )
        members = self._archive_members(
            artifact_path,
            expected_files,
            expected_directories,
            cancelled,
        )

        total_bytes = sum(
            int(entry["size_bytes"])
            for entry in expected_files.values()
        )
        completed_bytes = 0

        try:
            with zipfile.ZipFile(artifact_path, mode="r") as archive:
                for index, relative_path in enumerate(
                    sorted(expected_files, key=str.casefold),
                    1,
                ):
                    if cancelled():
                        raise CancelledError()

                    entry = expected_files[relative_path]
                    info = members[relative_path]
                    expected_size = int(entry["size_bytes"])
                    expected_hash = str(entry["sha256"])

                    if info.file_size != expected_size:
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            "ZIP entry size mismatch: "
                            f"{relative_path}",
                        )

                    digest = hashlib.sha256()
                    with archive.open(info, "r") as stream:
                        while True:
                            if cancelled():
                                raise CancelledError()

                            chunk = stream.read(HASH_CHUNK_SIZE)
                            if not chunk:
                                break

                            digest.update(chunk)

                    if digest.hexdigest() != expected_hash:
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            "ZIP entry hash mismatch: "
                            f"{relative_path}",
                        )

                    completed_bytes += expected_size
                    progress(
                        ProgressEvent(
                            operation_id,
                            "Verifying the ZIP version…",
                            relative_path,
                            index,
                            len(expected_files),
                            completed_bytes,
                            total_bytes,
                        )
                    )

        except zipfile.BadZipFile as exc:
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The ZIP artifact is corrupted or is not a ZIP file.",
                str(exc),
            ) from exc

    def extract_to(
        self,
        artifact_path: Path,
        manifest_entries: list[dict[str, Any]],
        destination_root: Path,
        cancelled: Callable[[], bool],
        progress: Callable[[ProgressEvent], None],
        operation_id: str,
    ) -> None:
        selected_paths = [
            str(entry["relative_path"])
            for entry in manifest_entries
            if entry.get("entry_type") == "file"
        ]
        self.extract_selected_to(
            artifact_path,
            manifest_entries,
            selected_paths,
            destination_root,
            cancelled,
            progress,
            operation_id,
        )

        directories = [
            entry
            for entry in manifest_entries
            if entry.get("entry_type") == "directory"
        ]

        for entry in directories:
            destination = PathPolicy.safe_join_under(
                destination_root,
                str(entry["relative_path"]),
            )
            destination.mkdir(parents=True, exist_ok=True)

    def extract_selected_to(
        self,
        artifact_path: Path,
        manifest_entries: list[dict[str, Any]],
        selected_relative_paths: list[str],
        destination_root: Path,
        cancelled: Callable[[], bool],
        progress: Callable[[ProgressEvent], None],
        operation_id: str,
    ) -> None:
        expected_files = self._manifest_files(manifest_entries)
        expected_directories = self._manifest_directories(
            manifest_entries
        )

        selected: list[str] = []
        selected_seen: set[str] = set()

        for value in selected_relative_paths:
            relative_path = self._safe_member_name(str(value))
            if relative_path not in expected_files:
                raise AppError(
                    "MANIFEST_INVALID",
                    "A selected file is not in the manifest: "
                    f"{relative_path}",
                )
            if relative_path not in selected_seen:
                selected_seen.add(relative_path)
                selected.append(relative_path)

        if not selected:
            raise AppError(
                "RESTORE_NOTHING_SELECTED",
                "No files selected for restoring.",
            )

        members = self._archive_members(
            artifact_path,
            expected_files,
            expected_directories,
            cancelled,
        )

        total_bytes = sum(
            int(expected_files[path]["size_bytes"])
            for path in selected
        )
        completed_bytes = 0

        try:
            with zipfile.ZipFile(artifact_path, mode="r") as archive:
                for index, relative_path in enumerate(
                    sorted(selected, key=str.casefold),
                    1,
                ):
                    if cancelled():
                        raise CancelledError()

                    entry = expected_files[relative_path]
                    info = members[relative_path]
                    destination = PathPolicy.safe_join_under(
                        destination_root,
                        relative_path,
                    )

                    if destination.exists() or destination.is_symlink():
                        raise AppError(
                            "RESTORE_DESTINATION_INVALID",
                            "The restore location is already in use: "
                            f"{relative_path}",
                        )

                    destination.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    digest = hashlib.sha256()
                    with (
                        archive.open(info, "r") as source,
                        destination.open("xb") as target,
                    ):
                        while True:
                            if cancelled():
                                raise CancelledError()

                            chunk = source.read(COPY_CHUNK_SIZE)
                            if not chunk:
                                break

                            target.write(chunk)
                            digest.update(chunk)

                    expected_size = int(entry["size_bytes"])
                    expected_hash = str(entry["sha256"])

                    if destination.stat().st_size != expected_size:
                        raise AppError(
                            "RESTORE_VERIFY_FAILED",
                            "The size of the restored file "
                            f"does not match: {relative_path}",
                        )

                    if digest.hexdigest() != expected_hash:
                        raise AppError(
                            "RESTORE_VERIFY_FAILED",
                            "The hash of the restored file "
                            f"does not match: {relative_path}",
                        )

                    completed_bytes += expected_size
                    progress(
                        ProgressEvent(
                            operation_id,
                            "Restoring selected files…",
                            str(destination),
                            index,
                            len(selected),
                            completed_bytes,
                            total_bytes,
                        )
                    )

        except zipfile.BadZipFile as exc:
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The ZIP artifact is corrupted or is not a ZIP file.",
                str(exc),
            ) from exc



class HealthService:
    ERROR_CODES = {
        "SOURCE_NOT_FOUND", "SOURCE_ACCESS_DENIED", "SOURCE_KIND_MISMATCH",
        "STORAGE_NOT_FOUND", "STORAGE_ACCESS_DENIED", "STORAGE_INSUFFICIENT_SPACE",
        "SNAPSHOT_CORRUPTED", "VERIFY_FAILED", "OPERATION_NEEDS_RECOVERY",
    }

    TITLES = {
        "SOURCE_NOT_FOUND": "Source not found",
        "SOURCE_ACCESS_DENIED": "No access to the source",
        "SOURCE_KIND_MISMATCH": "The source type changed",
        "STORAGE_NOT_FOUND": "Storage unavailable",
        "STORAGE_ACCESS_DENIED": "No permission to write to the storage",
        "STORAGE_INSUFFICIENT_SPACE": "Not enough space in the storage",
        "SNAPSHOT_CORRUPTED": "Version corrupted",
        "VERIFY_FAILED": "Version verification failed",
        "SOURCE_CHANGED_DURING_READ": "The source changed during the operation",
        "RETENTION_BLOCKED_BY_PINS": "Automatic cleanup is limited by pinned versions",
        "RETENTION_DELETE_FAILED": "Automatic cleanup error",
        "OPERATION_NEEDS_RECOVERY": "An interrupted operation needs a check",
    }

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def probe_source(self, plan: PlanDetails) -> SourceProbe:
        source = Path(plan.source_path)
        try:
            try:
                source.lstat()
            except FileNotFoundError:
                try:
                    candidate = self.repo.find_relocated_source(plan)
                except AppError as exc:
                    return SourceProbe(False, False, False, exc.code, exc.user_message)
                return SourceProbe(True, True, True, None, f"The same source was found nearby: {candidate.path}")
            if not plan.source_identity_json:
                return SourceProbe(True, False, False, "SOURCE_IDENTITY_UNCONFIRMED", "Confirm the plan source.")
            try:
                identity = confirmed_source_identity(source, plan.source_kind)
            except AppError as exc:
                return SourceProbe(exc.code == "SOURCE_LOCKED", False, exc.code == "SOURCE_LOCKED",
                                   exc.code, exc.user_message)
            if identity != plan.source_identity_json and not self.repo.refresh_replaced_file_identity(plan, identity):
                return SourceProbe(True, False, False, "SOURCE_IDENTITY_MISMATCH", "A different object is at the source path.")
            try:
                if source.is_file():
                    with source.open("rb"):
                        pass
                else:
                    next(source.iterdir(), None)
            except OSError as exc:
                locked = retryable_source_read(exc)
                return SourceProbe(True, False, True,
                                   "SOURCE_LOCKED" if locked else "SOURCE_ACCESS_DENIED",
                                   "The source file is busy." if locked else "No access to the source.")
            return SourceProbe(True, True, True, None, "The source is available.")
        except OSError as exc:
            locked = retryable_source_read(exc)
            return SourceProbe(locked, False, locked,
                               "SOURCE_LOCKED" if locked else "SOURCE_PROBE_ERROR", str(exc))

    def probe_storage(self, plan: PlanDetails) -> StorageProbe:
        storage = Path(plan.storage_path)
        try:
            if not storage.exists() or not storage.is_dir():
                return StorageProbe(False, False, None, "STORAGE_NOT_FOUND", "Storage not found.")
            probe = storage / "_amvibackup" / "temporary" / f".probe-{uuid.uuid4().hex}"
            try:
                probe.parent.mkdir(parents=True, exist_ok=True)
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except PermissionError:
                return StorageProbe(True, False, None, "STORAGE_ACCESS_DENIED", "No permission to write to the storage.")
            except OSError as exc:
                return StorageProbe(True, False, None, "STORAGE_PROBE_ERROR", str(exc))
            return StorageProbe(True, True, shutil.disk_usage(storage).free, None, "The storage is available.")
        except OSError as exc:
            return StorageProbe(False, False, None, "STORAGE_PROBE_ERROR", str(exc))

    def issue_from_error(
        self,
        error: AppError,
        plan_id: str | None,
        snapshot_id: str | None = None,
    ) -> IssueDraft:
        code = error.code
        suffix = snapshot_id if code == "SNAPSHOT_CORRUPTED" and snapshot_id else (plan_id or "global")
        severity = IssueSeverity.WARNING.value
        if code == "PLAN_DELETE_INCOMPLETE":
            severity = IssueSeverity.CRITICAL.value
        if code in {
            "SOURCE_IDENTITY_UNCONFIRMED",
            "SOURCE_IDENTITY_MISMATCH",
            "SOURCE_IDENTITY_UNAVAILABLE",
            "SOURCE_IDENTITY_AMBIGUOUS",
            "SOURCE_LOCKED",
        }:
            severity = IssueSeverity.ERROR.value
        if code in {"SNAPSHOT_CORRUPTED", "OPERATION_NEEDS_RECOVERY"}:
            severity = IssueSeverity.CRITICAL.value
        elif code in self.ERROR_CODES:
            severity = IssueSeverity.ERROR.value
        action = "Open plan" if plan_id else None
        if code == "OPERATION_NEEDS_RECOVERY":
            action = "Open log"
        return IssueDraft(
            dedupe_key=f"{code}:{suffix}",
            plan_id=plan_id,
            severity=severity,
            code=code,
            title=self.TITLES.get(code, code),
            detail=error.user_message,
            action_label=action,
        )

    def record_error(self, error: AppError, plan_id: str | None, snapshot_id: str | None = None) -> None:
        if error.code == "OPERATION_CANCELLED":
            return
        self.repo.upsert_issue(self.issue_from_error(error, plan_id, snapshot_id))

    def record_snapshot_corrupted(self, snapshot_id: str, plan_id: str, detail: str) -> None:
        self.repo.mark_snapshot_corrupted(snapshot_id)
        self.repo.upsert_issue(
            IssueDraft(
                dedupe_key=f"SNAPSHOT_CORRUPTED:{snapshot_id}",
                plan_id=plan_id,
                severity=IssueSeverity.CRITICAL.value,
                code="SNAPSHOT_CORRUPTED",
                title=self.TITLES["SNAPSHOT_CORRUPTED"],
                detail=detail,
                action_label="Open plan",
            )
        )

    def evaluate(self, plan: PlanDetails) -> PlanHealth:
        health, _, _ = self.evaluate_with_probes(plan)
        return health

    def evaluate_with_probes(
        self,
        plan: PlanDetails,
    ) -> tuple[PlanHealth, SourceProbe, StorageProbe]:
        source = self.probe_source(plan)
        storage = self.probe_storage(plan)
        health = self._evaluate_from_probes(plan, source, storage)
        return health, source, storage

    def _evaluate_from_probes(
        self,
        plan: PlanDetails,
        source: SourceProbe,
        storage: StorageProbe,
    ) -> PlanHealth:
        source_codes = (
            "SOURCE_NOT_FOUND",
            "SOURCE_ACCESS_DENIED",
            "SOURCE_KIND_MISMATCH",
            "SOURCE_PROBE_ERROR", "SOURCE_IDENTITY_UNCONFIRMED", "SOURCE_IDENTITY_MISMATCH",
            "SOURCE_IDENTITY_UNAVAILABLE",
            "SOURCE_IDENTITY_AMBIGUOUS",
            "SOURCE_LOCKED",
        )
        storage_codes = (
            "STORAGE_NOT_FOUND",
            "STORAGE_ACCESS_DENIED",
            "STORAGE_PROBE_ERROR",
        )

        for code in source_codes:
            if (code == "SOURCE_LOCKED" and plan.source_kind == SourceKind.FOLDER.value
                    and self.repo.last_backup_failed_from_lock(plan.id)):
                continue
            if code != source.error_code:
                self.repo.resolve_plan_issue(plan.id, code)

        for code in storage_codes:
            if code != storage.error_code:
                self.repo.resolve_plan_issue(plan.id, code)

        if source.error_code:
            self.record_error(
                AppError(source.error_code, source.detail),
                plan.id,
            )
        if storage.error_code:
            self.record_error(
                AppError(storage.error_code, storage.detail),
                plan.id,
            )

        snapshots = self.repo.snapshots(plan.id)
        committed = [snapshot for snapshot in snapshots if snapshot.state == "committed"]
        corrupted = [snapshot for snapshot in snapshots if snapshot.state == "corrupted"]
        for snapshot in corrupted:
            self.repo.upsert_issue(
                IssueDraft(
                    dedupe_key=f"SNAPSHOT_CORRUPTED:{snapshot.id}",
                    plan_id=plan.id,
                    severity=IssueSeverity.CRITICAL.value,
                    code="SNAPSHOT_CORRUPTED",
                    title=self.TITLES["SNAPSHOT_CORRUPTED"],
                    detail=f"Version #{snapshot.sequence_no} was marked as corrupted.",
                    action_label="Open plan",
                )
            )

        issues = self.repo.list_active_issues(plan.id)
        active_count = len(issues)
        last_snapshot = max((item.created_at_utc_ms for item in committed), default=None)

        if self.repo.plan_deletion_job(plan.id):
            if self.repo.unfinished_operations_for_plan(plan.id):
                state, title, detail = HealthState.RUNNING.value, "Deleting", "Plan data is being deleted."
            else:
                state, title, detail = HealthState.ERROR.value, "Deletion not finished", "Press 'Delete plan and data…' again."
        elif plan.state == PlanState.ARCHIVED.value:
            state, title, detail = HealthState.ARCHIVED.value, "Archive", "New versions are not created for this plan."
        elif plan.state == PlanState.PAUSED.value:
            state, title, detail = HealthState.PAUSED.value, "Paused", "The plan was paused manually."
        elif self.repo.unfinished_operations_for_plan(plan.id):
            state, title, detail = HealthState.RUNNING.value, "Running", "An operation on versions is running now."
        elif (
            source.error_code is not None
            or storage.error_code is not None
            or bool(corrupted)
            or self.repo.needs_recovery_for_plan(plan.id)
            or any(
                item.severity in (
                    IssueSeverity.ERROR.value,
                    IssueSeverity.CRITICAL.value,
                )
                for item in issues
            )
        ):
            state, title, detail = HealthState.ERROR.value, "Error", "Creating or restoring versions is not safe right now."
        elif not committed:
            state, title, detail = HealthState.UNPROTECTED.value, "No version", "The plan was created, but the first saved version has not been made yet."
        elif any(item.severity == IssueSeverity.WARNING.value for item in issues):
            state, title, detail = HealthState.ATTENTION.value, "Attention", "A saved version exists, but action is required."
        else:
            state, title, detail = HealthState.PROTECTED.value, "Protected", "A verifiable saved version exists."

        return PlanHealth(
            plan_id=plan.id,
            state=state,
            title=title,
            detail=detail,
            source_available=source.available,
            storage_available=storage.available,
            committed_snapshot_count=len(committed),
            last_snapshot_at_utc_ms=last_snapshot,
            last_operation_state=plan.last_operation_state,
            active_issue_count=active_count,
        )

    def evaluate_all(self) -> list[PlanHealth]:
        plans = self.repo.plan_summaries(False) + self.repo.plan_summaries(True)
        return [self.evaluate(self.repo.plan_details(plan.id)) for plan in plans]


class SnapshotService:
    def __init__(
        self,
        repo: Repository,
        storage: StorageService,
        health: HealthService,
        reserve_bytes: int = FREE_SPACE_MIN_RESERVE_BYTES,
        reserve_ratio: float = FREE_SPACE_RESERVE_RATIO,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.health = health
        self.reserve_bytes = reserve_bytes
        self.reserve_ratio = reserve_ratio
        self.zip_writer = ZipArtifactWriter()

    def _write_direct_artifact(
        self,
        temporary_root: Path,
        entries: list[dict[str, Any]],
        total_bytes: int,
        operation_id: str,
        cancelled: Callable[[], bool],
        progress: Callable[[ProgressEvent], None],
    ) -> tuple[list[dict[str, Any]], int, int]:
        data_root = temporary_root / StorageService.DIRECT_DATA_DIR_NAME
        data_root.mkdir(parents=True, exist_ok=False)

        manifest_entries: list[dict[str, Any]] = []
        files = [entry for entry in entries if entry["entry_type"] == "file"]
        completed_files = 0
        completed_bytes = 0

        for entry in entries:
            if cancelled():
                raise CancelledError()

            destination = data_root / Path(entry["relative_path"])

            if entry["entry_type"] == "directory":
                destination.mkdir(parents=True, exist_ok=True)
                manifest_entries.append(
                    {
                        "relative_path": entry["relative_path"],
                        "entry_type": "directory",
                        "size_bytes": 0,
                        "mtime_ns": entry["mtime_ns"],
                        "sha256": None,
                    }
                )
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()

            try:
                try:
                    stream = entry["source_path"].open("rb")
                except FileNotFoundError:
                    raise
                except OSError as exc:
                    raise AppError("SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                                   "Could not open the source file.", str(exc)) from exc
                with stream as source, destination.open("wb") as target:
                    assert_scanned_source_handle(source, entry)
                    copied_bytes = 0
                    while True:
                        if cancelled():
                            raise CancelledError()

                        try:
                            chunk = source.read(COPY_CHUNK_SIZE)
                        except OSError as exc:
                            raise AppError("SOURCE_LOCKED" if retryable_source_read(exc) else "SOURCE_ACCESS_DENIED",
                                           "Could not read the source file.", str(exc)) from exc
                        if not chunk:
                            break

                        target.write(chunk)
                        digest.update(chunk)
                        completed_bytes += len(chunk)
                        copied_bytes += len(chunk)

                        progress(
                            ProgressEvent(
                                operation_id,
                                "Creating the version…",
                                str(entry["source_path"]),
                                completed_files,
                                len(files),
                                completed_bytes,
                                total_bytes,
                            )
                        )

                    assert_scanned_source_handle(source, entry)
                    if copied_bytes != int(entry["size_bytes"]):
                        raise AppError("SOURCE_CHANGED_DURING_READ", "The size of the file read changed.")
                    target.flush()
                    try:
                        os.fsync(target.fileno())
                    except (OSError, ValueError):
                        logging.debug("fsync skipped for %s", destination, exc_info=True)

                shutil.copystat(
                    entry["source_path"],
                    destination,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise AppError(
                    "SOURCE_CHANGED_DURING_READ",
                    "The source file disappeared during copying.",
                    str(exc),
                ) from exc

            completed_files += 1
            manifest_entries.append(
                {
                    "relative_path": entry["relative_path"],
                    "entry_type": "file",
                    "size_bytes": entry["size_bytes"],
                    "mtime_ns": entry["mtime_ns"],
                    "sha256": digest.hexdigest(),
                }
            )

        manifest_entries.sort(key=lambda item: item["relative_path"].casefold())
        return manifest_entries, total_bytes, len(files)

    def _artifact_relpath(self, storage_mode: str) -> str:
        if storage_mode == StorageMode.DIRECT.value:
            return StorageService.DIRECT_DATA_DIR_NAME
        if storage_mode == StorageMode.ZIP.value:
            return StorageService.ZIP_ARTIFACT_NAME
        raise AppError(
            "STORAGE_MODE_INVALID",
            f"Unsupported storage mode: {storage_mode}",
        )

    def _retry_source_task(
        self,
        action: Callable[[], Any],
        operation_id: str,
        source_path: str,
        progress: Callable[[ProgressEvent], None],
        cancelled: Callable[[], bool],
        reset: Callable[[], None] | None = None,
    ) -> Any:
        for attempt in range(len(SOURCE_RETRY_DELAYS) + 1):
            if cancelled():
                raise CancelledError()
            try:
                return action()
            except AppError as exc:
                if exc.code != "SOURCE_LOCKED" or attempt == len(SOURCE_RETRY_DELAYS):
                    raise
                if reset is not None:
                    reset()
                progress(ProgressEvent(operation_id, "Waiting for a busy file…", source_path))
                deadline = time.monotonic() + SOURCE_RETRY_DELAYS[attempt]
                while time.monotonic() < deadline:
                    if cancelled():
                        raise CancelledError()
                    time.sleep(min(0.05, max(0.0, deadline-time.monotonic())))
        raise AssertionError("unreachable")
    def create(
        self,
        operation_id: str,
        plan_id: str,
        force: bool,
        progress: Callable[[ProgressEvent], None],
        cancelled: Callable[[], bool],
    ) -> OperationResult:
        temporary_root: Path | None = None
        snapshot_id: str | None = None
        lock_exhausted = False
        cancelled_clean = False
        owns_temporary = False

        try:
            self.repo.operation_state(operation_id, "running")
            plan = self.repo.plan_details(plan_id)

            if plan.state == PlanState.ARCHIVED.value:
                raise AppError("PLAN_ARCHIVED", "An archived plan cannot be run.")

            if plan.state == PlanState.PAUSED.value:
                raise AppError("PLAN_PAUSED", "A paused plan cannot be run.")

            if self.repo.needs_recovery_for_plan(plan_id):
                raise AppError(
                    "OPERATION_NEEDS_RECOVERY",
                    "This plan has an unfinished operation. "
                    "Check the log before creating a new version.",
                )

            if any(i.code == "RESTORE_RECOVERY_REVIEW_REQUIRED"
                   for i in self.repo.list_active_issues(plan_id)):
                raise AppError("OPERATION_NEEDS_RECOVERY", "Check the interrupted restore in the Health Center.")

            if plan.storage_mode not in {
                StorageMode.DIRECT.value,
                StorageMode.ZIP.value,
            }:
                raise AppError(
                    "STORAGE_MODE_INVALID",
                    f"The plan has an unsupported storage mode: {plan.storage_mode}",
                )

            source = self._retry_source_task(
                lambda: self.repo.resolve_source_for_backup(plan_id, cancelled),
                operation_id, plan.source_path, progress, cancelled,
            )
            plan = self.repo.plan_details(plan_id)
            storage_root = PathPolicy.validate_storage(
                plan.storage_path
            )
            PathPolicy.validate_topology(source, storage_root)
            self.storage.ensure_root(storage_root, plan.storage_root_id)

            entries, total_bytes, fingerprint_before = self._retry_source_task(
                lambda: Scanner.scan(source, cancelled, progress, operation_id),
                operation_id, str(source.path), progress, cancelled,
            )

            required_bytes = total_bytes + max(
                self.reserve_bytes,
                int(total_bytes * self.reserve_ratio),
            )
            if shutil.disk_usage(storage_root).free < required_bytes:
                raise AppError(
                    "STORAGE_INSUFFICIENT_SPACE",
                    f"Not enough space in the storage. "
                    f"At least {fmt_size(required_bytes)} is required.",
                )

            if not force and fingerprint_before == self.repo.last_fingerprint(plan_id):
                self.repo.operation_state(operation_id, "succeeded")
                self.repo.activity(
                    "info",
                    "BACKUP_SKIPPED_NO_CHANGES",
                    "No new version was created: no changes were found.",
                    plan_id,
                    operation_id,
                )
                return OperationResult(
                    operation_id,
                    True,
                    "No changes found: no new version was created.",
                    result_code="BACKUP_NO_CHANGES",
                )

            if cancelled():
                raise CancelledError()

            snapshot_id, sequence_no = self.repo.create_snapshot_creating(
                plan_id,
                operation_id,
            )

            temporary_root = self.storage.temporary_snapshot_dir(
                storage_root,
                operation_id,
                plan.storage_mode,
            )
            temporary_root.mkdir(parents=True, exist_ok=False)
            owns_temporary = True

            def write_artifact() -> tuple[list[dict[str, Any]], int, int]:
                if plan.storage_mode == StorageMode.DIRECT.value:
                    return self._write_direct_artifact(
                        temporary_root, entries, total_bytes, operation_id,
                        cancelled, progress,
                    )
                zip_artifact = temporary_root / StorageService.ZIP_ARTIFACT_NAME
                zip_result = self.zip_writer.write(
                    zip_artifact, source.kind.value, entries,
                    cancelled, progress, operation_id, total_bytes,
                )
                fsync_file(zip_artifact)
                return zip_result

            def reset_partial_artifact() -> None:
                if plan.storage_mode == StorageMode.DIRECT.value:
                    partial = temporary_root / StorageService.DIRECT_DATA_DIR_NAME
                    if partial.exists():
                        shutil.rmtree(partial)
                else:
                    (temporary_root / StorageService.ZIP_ARTIFACT_NAME).unlink(missing_ok=True)

            manifest_entries, stored_size, file_count = self._retry_source_task(
                write_artifact, operation_id, str(source.path),
                progress, cancelled, reset_partial_artifact,
            )

            _, _, fingerprint_after = self._retry_source_task(
                lambda: Scanner.scan(source, cancelled, progress, operation_id),
                operation_id, str(source.path), progress, cancelled,
            )
            self.repo.assert_source_identity(plan)
            if fingerprint_before != fingerprint_after:
                raise AppError(
                    "SOURCE_CHANGED_DURING_READ",
                    "The source changed while the version was being created.",
                )

            manifest = {
                "format_version": 1,
                "snapshot_id": snapshot_id,
                "created_at_utc": utc_iso(),
                "source_kind": source.kind.value,
                "storage_mode": plan.storage_mode,
                "entries": manifest_entries,
            }

            manifest_path = temporary_root / StorageService.MANIFEST_NAME
            manifest_path.write_text(
                json_text(manifest),
                encoding="utf-8",
            )
            fsync_file(manifest_path)
            manifest_hash = sha256_file(manifest_path, cancelled)

            snapshot_metadata = {
                "format_version": 1,
                "snapshot_id": snapshot_id,
                "plan_id": plan_id,
                "sequence_no": sequence_no,
                "created_at_utc": utc_iso(),
                "source_kind": source.kind.value,
                "storage_mode": plan.storage_mode,
                "artifact_relpath": self._artifact_relpath(plan.storage_mode),
                "manifest_relpath": StorageService.MANIFEST_NAME,
                "logical_size_bytes": total_bytes,
                "stored_size_bytes": stored_size,
                "file_count": file_count,
                "source_fingerprint": fingerprint_before,
                "manifest_sha256": manifest_hash,
            }

            metadata_path = temporary_root / StorageService.SNAPSHOT_METADATA_NAME
            metadata_path.write_text(
                json_text(snapshot_metadata),
                encoding="utf-8",
            )
            fsync_file(metadata_path)

            self.storage.ensure_plan_dir(storage_root, plan)
            final_root = self.storage.safe_snapshot_root(
                storage_root,
                plan,
                sequence_no,
            )
            final_root.parent.mkdir(parents=True, exist_ok=True)

            if final_root.exists():
                raise AppError(
                    "SNAPSHOT_PUBLISH_CONFLICT",
                    "Cannot publish the snapshot: the version folder already exists.",
                )

            progress(
                ProgressEvent(
                    operation_id,
                    "Publishing version…",
                    str(final_root),
                    cancellable=False,
                )
            )

            self.repo.assert_source_identity(plan)
            os.replace(temporary_root, final_root)
            temporary_root = None

            self.repo.commit_snapshot(
                snapshot_id,
                plan_id,
                operation_id,
                total_bytes,
                stored_size,
                file_count,
                fingerprint_before,
                manifest_hash,
            )

            try:
                self.storage.finalize_committed_artifact(
                    storage_root,
                    plan,
                    sequence_no,
                    plan.storage_mode,
                )
            except Exception as exc:
                logging.warning(
                    "Snapshot committed but read-only policy could not be fully applied: %s",
                    exc,
                    exc_info=True,
                )
                self.repo.activity(
                    "warning",
                    "SNAPSHOT_READONLY_NOT_APPLIED",
                    "The version was saved, but protection against accidental changes was not fully applied.",
                    plan_id,
                    operation_id,
                    {
                        "snapshot_id": snapshot_id,
                        "storage_mode": plan.storage_mode,
                        "error": str(exc),
                    },
                )

            try:
                RetentionService(
                    self.repo,
                    self.storage,
                    self.health,
                ).apply(plan_id)
            except Exception as exc:
                logging.exception("Retention failed")
                error = AppError(
                    "RETENTION_DELETE_FAILED",
                    "The new version was saved, but automatic cleanup of old versions did not finish.",
                    str(exc),
                )
                self.health.record_error(error, plan_id)
                self.repo.activity(
                    "warning",
                    error.code,
                    error.user_message,
                    plan_id,
                    operation_id,
                )

            self.repo.resolve_plan_issue(plan_id, "SOURCE_LOCKED")
            mode_label = "ZIP" if plan.storage_mode == StorageMode.ZIP.value else "direct"
            return OperationResult(
                operation_id,
                True,
                f"Version #{sequence_no} created ({mode_label}).",
                result_code="BACKUP_CREATED",
            )

        except CancelledError as exc:
            cancelled_clean = True
            self.repo.operation_state(operation_id, "cancelled", exc)
            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except AppError as exc:
            lock_exhausted = exc.code == "SOURCE_LOCKED"
            self.repo.operation_state(operation_id, "failed", exc)
            self.health.record_error(exc, plan_id, snapshot_id)
            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except Exception:
            logging.exception("Backup failure")
            error = AppError(
                "STORAGE_WRITE_FAILED",
                "Could not create a backup version.",
                traceback.format_exc(),
            )
            self.repo.operation_state(operation_id, "failed", error)
            self.health.record_error(error, plan_id, snapshot_id)
            return OperationResult(
                operation_id,
                False,
                error.user_message,
                error.code,
            )

        finally:
            if owns_temporary and temporary_root and temporary_root.exists():
                shutil.rmtree(temporary_root, ignore_errors=True)
            if owns_temporary and temporary_root and temporary_root.name == "zip-snapshot":
                try:
                    temporary_root.parent.rmdir()
                except OSError:
                    pass

            if snapshot_id:
                try:
                    if self.repo.snapshot(snapshot_id).state == "creating":
                        if (lock_exhausted or cancelled_clean) and (temporary_root is None or not temporary_root.exists()):
                            self.repo.mark_snapshot(snapshot_id, "deleted")
                        else:
                            self.repo.mark_snapshot(snapshot_id, "needs_recovery")
                except Exception:
                    logging.exception("Cannot mark unfinished snapshot")













class VerificationService:
    def __init__(
        self,
        repo: Repository,
        storage: StorageService,
        health: HealthService,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.health = health
        self.zip_reader = ZipArtifactReader()

    def artifact_paths(
        self,
        snapshot: SnapshotInfo,
    ) -> SnapshotArtifactPaths:
        plan = self.repo.plan_details(snapshot.plan_id)
        storage_root = Path(snapshot.storage_path)
        return self.storage.artifact_paths(
            storage_root,
            plan,
            snapshot.sequence_no,
        )

    def _read_manifest(
        self,
        snapshot: SnapshotInfo,
        manifest_path: Path,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        if not manifest_path.is_file():
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The snapshot manifest is missing from the storage.",
            )

        if sha256_file(manifest_path, cancelled) != snapshot.manifest_sha256:
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The snapshot manifest is corrupted or was edited manually.",
            )

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The snapshot manifest cannot be read.",
                str(exc),
            ) from exc

        if manifest.get("snapshot_id") != snapshot.id:
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The manifest belongs to a different version.",
            )

        if manifest.get("storage_mode") != snapshot.storage_mode:
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The manifest storage mode does not match the snapshot metadata.",
            )

        entries = manifest.get("entries")
        if not isinstance(entries, list):
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The snapshot manifest has an invalid structure.",
            )

        return manifest

    def list_snapshot_files(
        self,
        operation_id: str,
        snapshot_id: str,
        progress: Callable[[ProgressEvent], None],
        cancelled: Callable[[], bool],
    ) -> OperationResult:
        snapshot = None

        try:
            self.repo.operation_state(operation_id, "running")

            if cancelled():
                raise CancelledError()

            snapshot = self.repo.snapshot(snapshot_id)

            if snapshot.state != "committed":
                raise AppError(
                    "SNAPSHOT_NOT_COMMITTED",
                    "The version is unavailable.",
                )

            if snapshot.source_kind != SourceKind.FOLDER.value:
                raise AppError(
                    "RESTORE_FOLDER_ONLY",
                    "File selection is available only for a folder version.",
                )

            paths = self.artifact_paths(snapshot)
            manifest = self._read_manifest(
                snapshot,
                paths.manifest_path,
                cancelled,
            )

            result: list[str] = []
            seen: set[str] = set()

            for entry in manifest["entries"]:
                if cancelled():
                    raise CancelledError()

                if (
                    not isinstance(entry, dict)
                    or entry.get("entry_type")
                    not in {"file", "directory"}
                ):
                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "Invalid manifest entry.",
                    )

                relative = entry.get("relative_path")
                if not isinstance(relative, str):
                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "A manifest path is not a string.",
                    )

                PathPolicy.safe_join_under(
                    paths.snapshot_root,
                    relative,
                )

                key = relative.replace(chr(92), "/").casefold()
                if key in seen:
                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "The manifest has duplicate paths.",
                    )
                seen.add(key)

                if entry["entry_type"] == "file":
                    digest = entry.get("sha256")
                    size = entry.get("size_bytes")

                    if (
                        type(size) is not int
                        or size < 0
                        or not isinstance(digest, str)
                        or len(digest) != 64
                        or any(
                            char not in "0123456789abcdef"
                            for char in digest
                        )
                    ):
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            "The manifest has invalid file information.",
                        )

                    result.append(relative)

            current = self.repo.snapshot(snapshot_id)
            if (
                current.state != "committed"
                or current.manifest_sha256
                != snapshot.manifest_sha256
            ):
                raise AppError(
                    "SNAPSHOT_NOT_COMMITTED",
                    "The version changed during loading.",
                )

            self.repo.operation_state(
                operation_id,
                "succeeded",
            )
            return OperationResult(
                operation_id,
                True,
                "The file list was loaded.",
                result_code="SNAPSHOT_FILES_LOADED",
                result_data={
                    "snapshot_id": snapshot_id,
                    "manifest_sha256": snapshot.manifest_sha256,
                    "relative_paths": sorted(
                        result,
                        key=str.casefold,
                    ),
                },
            )

        except AppError as exc:
            state = (
                "cancelled"
                if exc.code == "OPERATION_CANCELLED"
                else "failed"
            )
            self.repo.operation_state(operation_id, state, exc)

            if (
                snapshot is not None
                and exc.code in {
                    "SNAPSHOT_CORRUPTED",
                    "MANIFEST_INVALID",
                }
            ):
                self.health.record_snapshot_corrupted(
                    snapshot.id,
                    snapshot.plan_id,
                    exc.user_message,
                )

            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

    def verify(
        self,
        operation_id: str,
        snapshot_id: str,
        progress: Callable[[ProgressEvent], None],
        cancelled: Callable[[], bool],
    ) -> OperationResult:
        snapshot: SnapshotInfo | None = None

        try:
            self.repo.operation_state(operation_id, "running")
            snapshot = self.repo.snapshot(snapshot_id)

            if snapshot.state != "committed":
                raise AppError(
                    "SNAPSHOT_NOT_COMMITTED",
                    "Only an available saved version can be verified.",
                )

            if snapshot.storage_mode not in {
                StorageMode.DIRECT.value,
                StorageMode.ZIP.value,
            }:
                raise AppError(
                    "STORAGE_MODE_INVALID",
                    f"The version has an unsupported storage mode: {snapshot.storage_mode}",
                )

            paths = self.artifact_paths(snapshot)
            manifest = self._read_manifest(
                snapshot,
                paths.manifest_path,
                cancelled,
            )
            entries = manifest["entries"]

            if snapshot.storage_mode == StorageMode.DIRECT.value:
                if not paths.snapshot_root.is_dir() or not paths.direct_data_root.is_dir():
                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "The files of the direct snapshot are missing from the storage.",
                    )

                files = [
                    entry
                    for entry in entries
                    if entry.get("entry_type") == "file"
                ]
                total_bytes = sum(int(entry["size_bytes"]) for entry in files)
                completed_bytes = 0

                for index, entry in enumerate(files, 1):
                    if cancelled():
                        raise CancelledError()

                    relative_path = str(entry.get("relative_path", ""))
                    target = PathPolicy.safe_join_under(
                        paths.direct_data_root,
                        relative_path,
                    )
                    expected_size = int(entry.get("size_bytes", -1))
                    expected_hash = str(entry.get("sha256", ""))

                    if not target.is_file():
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            f"A file is missing from the snapshot: {relative_path}",
                        )

                    if target.stat().st_size != expected_size:
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            f"File size mismatch: {relative_path}",
                        )

                    if not expected_hash or sha256_file(target, cancelled) != expected_hash:
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            f"File hash mismatch: {relative_path}",
                        )

                    completed_bytes += expected_size
                    progress(
                        ProgressEvent(
                            operation_id,
                            "Verifying the version…",
                            str(target),
                            index,
                            len(files),
                            completed_bytes,
                            total_bytes,
                        )
                    )

            else:
                self.zip_reader.verify(
                    paths.zip_artifact,
                    entries,
                    cancelled,
                    progress,
                    operation_id,
                )

            self.repo.update_verification(snapshot_id, True)
            self.repo.operation_state(operation_id, "succeeded")
            self.repo.activity(
                "info",
                "VERIFY_PASSED",
                "Version verification finished successfully.",
                snapshot.plan_id,
                operation_id,
            )
            self.repo.resolve_issue(f"SNAPSHOT_CORRUPTED:{snapshot_id}")

            return OperationResult(
                operation_id,
                True,
                "The version was verified successfully.",
                result_code="VERIFY_SUCCEEDED",
            )

        except CancelledError as exc:
            self.repo.operation_state(operation_id, "cancelled", exc)
            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except AppError as exc:
            if snapshot is not None:
                self.health.record_snapshot_corrupted(
                    snapshot_id,
                    snapshot.plan_id,
                    exc.user_message,
                )
                self.repo.activity(
                    "error",
                    "VERIFY_FAILED",
                    exc.user_message,
                    snapshot.plan_id,
                    operation_id,
                    {
                        "snapshot_id": snapshot_id,
                        "code": exc.code,
                    },
                )

            self.repo.operation_state(operation_id, "failed", exc)
            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except Exception:
            logging.exception("Verify failure")
            error = AppError(
                "SNAPSHOT_CORRUPTED",
                "Could not verify the version.",
                traceback.format_exc(),
            )

            if snapshot is not None:
                self.health.record_snapshot_corrupted(
                    snapshot_id,
                    snapshot.plan_id,
                    error.user_message,
                )

            self.repo.operation_state(operation_id, "failed", error)
            return OperationResult(
                operation_id,
                False,
                error.user_message,
                error.code,
            )








class RestoreTestService:
    """Restore test through the production restore path in a private temp folder.

    Two scopes are kept honestly separate: "full" (everything extracted and checked) and
    "sample" (a limited, rotating set of files). A sample never replaces a valid full proof
    and is never reported as a full check. Every attempt (success, failure, cancel,
    no space) is recorded separately, so an older success never hides a newer problem.
    A temporary-folder cleanup failure is not a backup corruption.
    """
    PROOF_PREFIX = "recovery19.proof."
    ATTEMPT_PREFIX = "recovery19.attempt."
    CURSOR_PREFIX = "recovery19.cursor."
    TEMP_PREFIX = "amvibackup-recovery19-"
    SPACE_MARGIN = 64 * 1024 * 1024
    SAMPLE_MAX_FILES = 40
    SAMPLE_MAX_BYTES = 64 * 1024 * 1024

    def __init__(self, repo, restore_service):
        self.repo = repo
        self.restore_service = restore_service

    @staticmethod
    def _valid_stamp(value):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    def proof(self, snapshot):
        value = self.repo.get_setting(self.PROOF_PREFIX + snapshot.id, None)
        if not isinstance(value, dict):
            return None
        if (snapshot.state != "committed"
                or value.get("manifest_sha256") != snapshot.manifest_sha256
                or value.get("snapshot_id") != snapshot.id
                or not self._valid_stamp(value.get("tested_at_utc_ms"))):
            return None
        return value

    def last_attempt(self, snapshot):
        value = self.repo.get_setting(self.ATTEMPT_PREFIX + snapshot.id, None)
        if not isinstance(value, dict):
            return None
        if (value.get("snapshot_id") != snapshot.id
                or value.get("manifest_sha256") != snapshot.manifest_sha256
                or not self._valid_stamp(value.get("finished_at_utc_ms"))):
            return None
        return value

    def status(self, snapshot):
        proof = self.proof(snapshot)
        attempt = self.last_attempt(snapshot)
        if proof is None:
            return {"state": "failed" if attempt else "unproven", "proof": None, "attempt": attempt}
        if (attempt and attempt.get("status") in {"failed", "cancelled", "no_space"}
                and attempt["finished_at_utc_ms"] > proof["tested_at_utc_ms"]):
            return {"state": "proven_then_failed", "proof": proof, "attempt": attempt}
        return {"state": "proven", "proof": proof, "attempt": attempt}

    def _record(self, snapshot, status, started, t0, code=None, scope="full", files=None, size=None):
        record = {"snapshot_id": snapshot.id, "manifest_sha256": snapshot.manifest_sha256,
                  "started_at_utc_ms": started, "finished_at_utc_ms": utc_ms(),
                  "duration_ms": int((time.monotonic() - t0) * 1000), "status": status,
                  "scope": scope,
                  "files": int(snapshot.file_count or 0) if files is None else int(files),
                  "total_files": int(snapshot.file_count or 0),
                  "bytes": int(snapshot.logical_size_bytes or 0) if size is None else int(size),
                  "error_code": code}
        try:
            self.repo.set_setting(self.ATTEMPT_PREFIX + snapshot.id, record)
        except Exception:
            logging.exception("Cannot record restore test attempt")
        return record

    def _cleanup(self, path):
        # Delete only our own freshly created directory directly under the system temp dir.
        try:
            target = Path(path)
            root = Path(tempfile.gettempdir()).resolve()
            if (not target.name.startswith(self.TEMP_PREFIX)
                    or target.resolve().parent != root or target.is_symlink()):
                return False
            if target.exists():
                set_writable_best_effort(target)
                shutil.rmtree(target)
            return not target.exists()
        except OSError:
            return False

    def pick_sample(self, snapshot, entries):
        """Deterministic rotating sample: continue after the files checked last time."""
        files = sorted((e for e in entries if e.get("entry_type") == "file"),
                       key=lambda e: str(e["relative_path"]).casefold())
        if not files:
            return []
        cursor = self.repo.get_setting(self.CURSOR_PREFIX + snapshot.id, 0)
        cursor = cursor if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0 else 0
        start = cursor % len(files)
        chosen, total, count = [], 0, 0
        for step in range(len(files)):
            entry = files[(start + step) % len(files)]
            size = int(entry.get("size_bytes", 0))
            if count >= self.SAMPLE_MAX_FILES:
                break
            if total + size > self.SAMPLE_MAX_BYTES:
                if chosen:
                    continue
            chosen.append(entry)
            total += size
            count += 1
            if total >= self.SAMPLE_MAX_BYTES:
                break
        if not chosen:
            chosen = [min(files, key=lambda e: int(e.get("size_bytes", 0)))]
        last_index = files.index(chosen[-1])
        try:
            self.repo.set_setting(self.CURSOR_PREFIX + snapshot.id, last_index + 1)
        except Exception:
            logging.exception("Cannot store restore test cursor")
        return chosen

    def run(self, operation_id, snapshot_id, progress, cancelled, mode="full"):
        snapshot = self.repo.snapshot(snapshot_id)
        if snapshot.state != "committed":
            raise AppError("SNAPSHOT_NOT_COMMITTED", "The version is not available for restoring.")
        mode = "sample" if mode == "sample" else "full"
        started, t0 = utc_ms(), time.monotonic()
        scope = "full"
        selected_paths = None
        files_checked = int(snapshot.file_count or 0)
        bytes_checked = int(snapshot.logical_size_bytes or 0)
        if cancelled():
            self._record(snapshot, "cancelled", started, t0, scope=mode)
            raise CancelledError()
        if mode == "sample" and snapshot.source_kind == SourceKind.FOLDER.value:
            try:
                paths = self.restore_service.verifier.artifact_paths(snapshot)
                manifest = self.restore_service.verifier._read_manifest(
                    snapshot, paths.manifest_path, cancelled)
                chosen = self.pick_sample(snapshot, manifest["entries"])
            except CancelledError:
                self._record(snapshot, "cancelled", started, t0, scope="sample")
                raise
            except AppError as exc:
                self._record(snapshot, "failed", started, t0, exc.code, scope="sample")
                raise
            total_files = sum(1 for e in manifest["entries"] if e.get("entry_type") == "file")
            if chosen and len(chosen) < total_files:
                scope = "sample"
                selected_paths = [str(e["relative_path"]) for e in chosen]
                files_checked = len(chosen)
                bytes_checked = sum(int(e.get("size_bytes", 0)) for e in chosen)
        needed = (bytes_checked if scope == "sample" else int(snapshot.logical_size_bytes or 0)) + self.SPACE_MARGIN
        try:
            free = shutil.disk_usage(tempfile.gettempdir()).free
        except OSError:
            free = None
        if free is not None and free < needed:
            self._record(snapshot, "no_space", started, t0, "RESTORE_TEST_NO_SPACE",
                         scope, files_checked, bytes_checked)
            raise AppError("RESTORE_TEST_NO_SPACE",
                           "Not enough space in the temporary folder for a test restore.")
        temporary = None
        try:
            temporary = tempfile.mkdtemp(prefix=self.TEMP_PREFIX)
            if selected_paths is None:
                result = self.restore_service.restore_to_directory(
                    operation_id, snapshot_id, temporary, progress, cancelled)
            else:
                result = self.restore_service.restore_selected_files(
                    operation_id, snapshot_id, selected_paths, temporary, progress, cancelled)
        except CancelledError:
            self._cleanup(temporary) if temporary else None
            self._record(snapshot, "cancelled", started, t0, None, scope, files_checked, bytes_checked)
            raise
        except AppError as exc:
            self._cleanup(temporary) if temporary else None
            self._record(snapshot, "failed", started, t0, exc.code, scope, files_checked, bytes_checked)
            raise
        except OSError as exc:
            self._cleanup(temporary) if temporary else None
            self._record(snapshot, "failed", started, t0, "RESTORE_TEST_FAILED", scope, files_checked, bytes_checked)
            raise AppError("RESTORE_TEST_FAILED", "Could not perform the test restore.", str(exc)) from exc
        removed = self._cleanup(temporary)
        if not result.success:
            was_cancelled = bool(cancelled())
            self._record(snapshot, "cancelled" if was_cancelled else "failed", started, t0,
                         None if was_cancelled else getattr(result, "error_code", None),
                         scope, files_checked, bytes_checked)
            return result
        current = self.repo.snapshot(snapshot_id)
        if current.state != "committed" or current.manifest_sha256 != snapshot.manifest_sha256:
            self._record(snapshot, "failed", started, t0, "RESTORE_TEST_STALE", scope, files_checked, bytes_checked)
            raise AppError("RESTORE_TEST_STALE", "The version changed during the test.")
        finished = utc_ms()
        duration = int((time.monotonic() - t0) * 1000)
        proof = {"snapshot_id": snapshot.id, "manifest_sha256": snapshot.manifest_sha256,
                 "tested_at_utc_ms": finished, "operation_id": operation_id, "scope": scope,
                 "files": files_checked, "total_files": int(snapshot.file_count or 0),
                 "bytes": bytes_checked, "duration_ms": duration,
                 "temporary_output_removed": bool(removed)}
        attempt = {"snapshot_id": snapshot.id, "manifest_sha256": snapshot.manifest_sha256,
                   "started_at_utc_ms": started, "finished_at_utc_ms": finished,
                   "duration_ms": duration,
                   "status": "succeeded" if removed else "cleanup_pending", "scope": scope,
                   "files": files_checked, "total_files": proof["total_files"],
                   "bytes": bytes_checked, "error_code": None}
        existing = self.proof(snapshot)
        keep_full = scope == "sample" and existing is not None and existing.get("scope", "full") == "full"
        with self.repo.db.transaction() as conn:
            items = [(self.ATTEMPT_PREFIX + snapshot.id, attempt)]
            if not keep_full:
                items.insert(0, (self.PROOF_PREFIX + snapshot.id, proof))
            for key, value in items:
                conn.execute(
                    "INSERT INTO AppSettings(key,value_json,updated_at_utc_ms) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                    "updated_at_utc_ms=excluded.updated_at_utc_ms",
                    (key, json.dumps(value), finished))
        message = "The test restore passed."
        if scope == "sample":
            message = f"Sampled test passed: {files_checked} of {proof['total_files']} files."
        if not removed:
            message += "  The temporary folder could not be deleted: " + str(temporary)
        return OperationResult(operation_id, True, message,
                               result_code="RESTORE_TEST_SUCCEEDED", result_data=proof)


def unique_restored_path(
    parent: Path,
    name: str,
    is_file: bool,
    stamp: str,
    taken: "set[Path] | None" = None,
    force_suffix: bool = False,
) -> Path:
    """Free path for a restored item: the original name if it is free, otherwise NAME-restored-STAMP.

    Nothing that already exists is ever overwritten. `taken` holds names reserved earlier in the same restore.
    """
    reserved = taken or set()

    def busy(path: Path) -> bool:
        return path.exists() or path.is_symlink() or path in reserved

    first = parent / name
    if not force_suffix and not busy(first):
        return first
    if is_file:
        base, suffix = Path(name).stem, Path(name).suffix
    else:
        base, suffix = name, ""
    candidate = parent / f"{base}-restored-{stamp}{suffix}"
    counter = 2
    while busy(candidate):
        candidate = parent / f"{base}-restored-{stamp}-{counter}{suffix}"
        counter += 1
    return candidate


def restore_folder_is_allowed(folder: Path, snapshot: "SnapshotInfo") -> bool:
    """A restore folder must not be inside the backup location or inside the protected folder."""
    return not (
        PathPolicy.inside(folder, Path(snapshot.storage_path))
        or PathPolicy.inside(folder, Path(snapshot.source_path))
    )


def _restore_base_folders() -> list[Path]:
    bases: list[Path] = []
    try:
        from PySide6.QtCore import QStandardPaths

        for location in (
            QStandardPaths.StandardLocation.DocumentsLocation,
            QStandardPaths.StandardLocation.DesktopLocation,
        ):
            text = QStandardPaths.writableLocation(location)
            if text:
                bases.append(Path(text))
    except Exception:
        pass
    bases.extend([Path.home() / "Documents", Path.home() / "Desktop", Path.home()])
    unique: list[Path] = []
    for base in bases:
        if base not in unique:
            unique.append(base)
    return unique


def suggest_restore_folder(repo: "Repository", snapshot: "SnapshotInfo") -> str:
    """Folder offered for restoring: the last one used, else <Documents>\\AmviBackUp restored (never inside source or storage)."""
    try:
        last = repo.get_setting(SETTING_LAST_RESTORE_FOLDER, None)
    except AppError:
        last = None
    if isinstance(last, str) and last:
        candidate = Path(last)
        if candidate.is_absolute() and candidate.is_dir() and restore_folder_is_allowed(candidate, snapshot):
            return str(candidate)
    for base in _restore_base_folders():
        candidate = base / RESTORE_FOLDER_NAME
        if restore_folder_is_allowed(candidate, snapshot):
            return str(candidate)
    return ""


def move_restore_no_replace(source: Path, destination: Path) -> None:
    """Publish a checked restore without replacing an existing path, even if it appeared just now.

    Windows os.rename refuses to replace. On Linux use renameat2(RENAME_NOREPLACE).
    On other systems the hard-link fallback works for files only; fail closed for directories.
    """
    if os.name == "nt":
        os.rename(source, destination)
        return
    if sys.platform.startswith("linux"):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is not None:
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
                return
            code = ctypes.get_errno()
            if code not in (errno.ENOSYS, errno.EINVAL):
                raise OSError(code, os.strerror(code), str(destination))
    if source.is_file() and not source.is_symlink():
        os.link(source, destination)
        source.unlink()
        return
    raise OSError(errno.ENOTSUP, "Safe no-replace directory move is unavailable", str(destination))


def checked_restore_target(root: Path, relative: str) -> Path:
    """Validate the whole destination chain; never follow a redirect inside the chosen folder."""
    result = PathPolicy.safe_join_under(root, relative)
    lexical = root / Path(relative.replace("\\", "/"))
    current = root
    for part in lexical.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise AppError("RESTORE_DESTINATION_REDIRECT", dictionary_text("error.RESTORE_DESTINATION_REDIRECT"))
        if current != lexical and current.exists() and not current.is_dir():
            raise AppError("RESTORE_DESTINATION_NOT_FOLDER", dictionary_text("error.RESTORE_DESTINATION_NOT_FOLDER"))
    return result


class RestoreService:
    def __init__(
        self,
        repo: Repository,
        verifier: VerificationService,
        health: HealthService,
    ) -> None:
        self.repo = repo
        self.verifier = verifier
        self.health = health

    def _unique_sibling_path(
        self,
        source: Path,
        source_kind: str,
        stamp: str,
    ) -> Path:
        parent = source.parent

        if source_kind == SourceKind.FILE.value:
            candidate = (
                parent
                / f"{source.stem}-restored-{stamp}{source.suffix}"
            )
            suffix = 2
            while candidate.exists():
                candidate = (
                    parent
                    / f"{source.stem}-restored-{stamp}-{suffix}"
                    f"{source.suffix}"
                )
                suffix += 1
            return candidate

        candidate = parent / f"{source.name}-restored-{stamp}"
        suffix = 2
        while candidate.exists():
            candidate = (
                parent / f"{source.name}-restored-{stamp}-{suffix}"
            )
            suffix += 1
        return candidate

    def _verify_restored_output(
        self,
        root: Path,
        source_kind: str,
        entries: list[dict[str, Any]],
        cancelled: Callable[[], bool],
    ) -> None:
        for entry in entries:
            if entry.get("entry_type") != "file":
                continue

            if cancelled():
                raise CancelledError()

            relative_path = str(entry["relative_path"])
            expected_size = int(entry["size_bytes"])
            expected_hash = str(entry["sha256"])

            target = (
                root
                if source_kind == SourceKind.FILE.value
                else PathPolicy.safe_join_under(root, relative_path)
            )

            if not target.is_file():
                raise AppError(
                    "RESTORE_VERIFY_FAILED",
                    "A file is missing from the restored data: "
                    f"{relative_path}",
                )

            if target.stat().st_size != expected_size:
                raise AppError(
                    "RESTORE_VERIFY_FAILED",
                    "The restored file size does not match: "
                    f"{relative_path}",
                )

            if sha256_file(target, cancelled) != expected_hash:
                raise AppError(
                    "RESTORE_VERIFY_FAILED",
                    "The restored file hash does not match: "
                    f"{relative_path}",
                )

    def _selected_file_entries(
        self,
        entries: list[dict[str, Any]],
        selected_relative_paths: list[str],
    ) -> list[dict[str, Any]]:
        manifest_files: dict[str, dict[str, Any]] = {}

        for entry in entries:
            if entry.get("entry_type") != "file":
                continue

            relative_path = str(entry.get("relative_path", ""))
            PathPolicy.safe_join_under(Path("."), relative_path)

            if relative_path in manifest_files:
                raise AppError(
                    "MANIFEST_INVALID",
                    "The manifest contains a duplicate file: "
                    f"{relative_path}",
                )

            manifest_files[relative_path] = entry

        selected: list[dict[str, Any]] = []
        selected_seen: set[str] = set()

        for value in selected_relative_paths:
            relative_path = str(value)
            PathPolicy.safe_join_under(Path("."), relative_path)

            if relative_path not in manifest_files:
                raise AppError(
                    "MANIFEST_INVALID",
                    "A selected file is not in the manifest: "
                    f"{relative_path}",
                )

            if relative_path not in selected_seen:
                selected_seen.add(relative_path)
                selected.append(manifest_files[relative_path])

        if not selected:
            raise AppError(
                "RESTORE_NOTHING_SELECTED",
                "No files selected for restoring.",
            )

        return selected

    def _validate_selected_destination(
        self,
        snapshot: SnapshotInfo,
        destination_text: str,
    ) -> Path:
        if not isinstance(destination_text, str) or not destination_text.strip():
            raise AppError("RESTORE_DESTINATION_MISSING", dictionary_text("error.RESTORE_DESTINATION_MISSING"))
        try:
            PathPolicy._reject_special(destination_text, storage=True)
        except AppError as exc:
            raise AppError("RESTORE_DESTINATION_NOT_FOLDER", dictionary_text("error.RESTORE_DESTINATION_NOT_FOLDER"), str(exc)) from exc

        destination = Path(destination_text.strip()).expanduser()
        if destination.is_symlink() or getattr(destination, "is_junction", lambda: False)():
            raise AppError("RESTORE_DESTINATION_REDIRECT", dictionary_text("error.RESTORE_DESTINATION_REDIRECT"))

        if (
            not destination.is_absolute()
            or not destination.is_dir()
        ):
            raise AppError(
                "RESTORE_DESTINATION_NOT_FOLDER",
                dictionary_text("error.RESTORE_DESTINATION_NOT_FOLDER"),
            )

        if getattr(destination, "is_junction", lambda: False)():
            raise AppError(
                "RESTORE_DESTINATION_REDIRECT",
                dictionary_text("error.RESTORE_DESTINATION_REDIRECT"),
            )

        destination = destination.resolve(strict=True)

        if PathPolicy.inside(
            destination,
            Path(snapshot.storage_path),
        ):
            raise AppError(
                "RESTORE_DESTINATION_IN_STORAGE",
                dictionary_text("error.RESTORE_DESTINATION_IN_STORAGE"),
            )

        if PathPolicy.inside(
            destination,
            Path(snapshot.source_path),
        ):
            raise AppError(
                "RESTORE_DESTINATION_IN_SOURCE",
                dictionary_text("error.RESTORE_DESTINATION_IN_SOURCE"),
            )

        return destination

    def _copy_selected_direct(
        self,
        artifact_root: Path,
        selected_entries: list[dict[str, Any]],
        destination_root: Path,
        operation_id: str,
        cancelled: Callable[[], bool],
        progress: Callable[[ProgressEvent], None],
    ) -> None:
        total_bytes = sum(
            int(entry["size_bytes"])
            for entry in selected_entries
        )
        completed_bytes = 0

        for index, entry in enumerate(
            sorted(
                selected_entries,
                key=lambda item: str(
                    item["relative_path"]
                ).casefold(),
            ),
            1,
        ):
            if cancelled():
                raise CancelledError()

            relative_path = str(entry["relative_path"])
            source = PathPolicy.safe_join_under(
                artifact_root,
                relative_path,
            )
            destination = PathPolicy.safe_join_under(
                destination_root,
                relative_path,
            )

            if (
                not source.is_file()
                or source.is_symlink()
            ):
                raise AppError(
                    "SNAPSHOT_CORRUPTED",
                    "A file is missing from the saved version: "
                    f"{relative_path}",
                )

            if destination.exists() or destination.is_symlink():
                raise AppError(
                    "RESTORE_DESTINATION_INVALID",
                    "The restore location is already in use: "
                    f"{relative_path}",
                )

            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            digest = hashlib.sha256()

            with (
                source.open("rb") as input_stream,
                destination.open("xb") as output_stream,
            ):
                while True:
                    if cancelled():
                        raise CancelledError()

                    chunk = input_stream.read(COPY_CHUNK_SIZE)
                    if not chunk:
                        break

                    output_stream.write(chunk)
                    digest.update(chunk)

            expected_size = int(entry["size_bytes"])
            expected_hash = str(entry["sha256"])

            if destination.stat().st_size != expected_size:
                raise AppError(
                    "RESTORE_VERIFY_FAILED",
                    "The restored file size does not match: "
                    f"{relative_path}",
                )

            if digest.hexdigest() != expected_hash:
                raise AppError(
                    "RESTORE_VERIFY_FAILED",
                    "The restored file hash does not match: "
                    f"{relative_path}",
                )

            completed_bytes += expected_size
            progress(
                ProgressEvent(
                    operation_id,
                    "Restoring selected files…",
                    str(destination),
                    index,
                    len(selected_entries),
                    completed_bytes,
                    total_bytes,
                )
            )

    @staticmethod
    def _saved_file_name(entries: list[dict[str, Any]]) -> str:
        files = [
            entry for entry in entries
            if entry.get("entry_type") == "file"
        ]
        if len(files) != 1:
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "A file version must contain exactly one file.",
            )

        name = files[0].get("relative_path")
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or chr(92) in name
            or ":" in name
        ):
            raise AppError(
                "SNAPSHOT_CORRUPTED",
                "The manifest contains an unsafe file name.",
            )
        return name

    def restore(
        self,
        operation_id: str,
        snapshot_id: str,
        progress: Callable[[ProgressEvent], None],
        cancelled: Callable[[], bool],
    ) -> OperationResult:
        temporary: Path | None = None
        snapshot: SnapshotInfo | None = None

        try:
            self.repo.operation_state(operation_id, "running")
            snapshot = self.repo.snapshot(snapshot_id)

            if snapshot.state != "committed":
                raise AppError(
                    "SNAPSHOT_CORRUPTED",
                    "Restoring is available only "
                    "for an undamaged version.",
                )

            if snapshot.storage_mode not in {
                StorageMode.DIRECT.value,
                StorageMode.ZIP.value,
            }:
                raise AppError(
                    "STORAGE_MODE_INVALID",
                    "The version storage mode is not supported.",
                )

            paths = self.verifier.artifact_paths(snapshot)
            manifest = self.verifier._read_manifest(
                snapshot,
                paths.manifest_path,
                cancelled,
            )
            entries = manifest["entries"]
            saved_name = (
                self._saved_file_name(entries)
                if snapshot.source_kind == SourceKind.FILE.value
                else ""
            )

            source = Path(snapshot.source_path)
            parent = source.parent
            if not parent.exists():
                raise AppError(
                    "RESTORE_SOURCE_PARENT_MISSING",
                    "The folder next to the original "
                    "no longer exists.",
                )

            stamp = datetime.now().strftime(
                "%Y-%m-%d-%H%M%S"
            )
            final = self._unique_sibling_path(
                source,
                snapshot.source_kind,
                stamp,
            )
            temporary = (
                parent
                / f".amvibackup-restore-{operation_id}.tmp"
            )

            if temporary.exists():
                raise AppError(
                    "RESTORE_TEMP_EXISTS",
                    "The temporary restore path "
                    "already exists.",
                )

            if snapshot.storage_mode == StorageMode.DIRECT.value:
                if not paths.direct_data_root.is_dir():
                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "The direct snapshot data is missing.",
                    )

                if snapshot.source_kind == SourceKind.FILE.value:
                    backup_file = (
                        paths.direct_data_root / saved_name
                    )
                    if not backup_file.is_file():
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            "The original file is missing from the backup.",
                        )
                    shutil.copy2(backup_file, temporary)
                else:
                    shutil.copytree(
                        paths.direct_data_root,
                        temporary,
                    )

            else:
                if snapshot.source_kind == SourceKind.FILE.value:
                    temporary.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    temporary.mkdir(
                        parents=True,
                        exist_ok=False,
                    )

                    self.verifier.zip_reader.extract_to(
                        paths.zip_artifact,
                        entries,
                        temporary,
                        cancelled,
                        progress,
                        operation_id,
                    )

                    restored_file = temporary / saved_name
                    if not restored_file.is_file():
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            "The ZIP backup is missing "
                            "the original file.",
                        )

                    file_temporary = (
                        parent
                        / (
                            ".amvibackup-restore-file-"
                            f"{operation_id}.tmp"
                        )
                    )
                    if file_temporary.exists():
                        raise AppError(
                            "RESTORE_TEMP_EXISTS",
                            "The temporary restore file "
                            "already exists.",
                        )

                    os.replace(
                        restored_file,
                        file_temporary,
                    )
                    shutil.rmtree(
                        temporary,
                        ignore_errors=True,
                    )
                    temporary = file_temporary
                else:
                    temporary.mkdir(
                        parents=True,
                        exist_ok=False,
                    )
                    self.verifier.zip_reader.extract_to(
                        paths.zip_artifact,
                        entries,
                        temporary,
                        cancelled,
                        progress,
                        operation_id,
                    )

            self._verify_restored_output(
                temporary,
                snapshot.source_kind,
                entries,
                cancelled,
            )

            os.replace(temporary, final)
            temporary = None

            self.repo.operation_state(
                operation_id,
                "succeeded",
            )
            self.repo.activity(
                "info",
                "RESTORE_SUCCEEDED",
                f"Version restored: {final}",
                snapshot.plan_id,
                operation_id,
            )

            return OperationResult(
                operation_id,
                True,
                f"Restored: {final}",
                result_code="RESTORE_SUCCEEDED",
            )

        except CancelledError as exc:
            self.repo.operation_state(
                operation_id,
                "cancelled",
                exc,
            )
            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except AppError as exc:
            self.repo.operation_state(
                operation_id,
                "failed",
                exc,
            )

            if snapshot is not None:
                if exc.code == "SNAPSHOT_CORRUPTED":
                    self.health.record_snapshot_corrupted(
                        snapshot.id,
                        snapshot.plan_id,
                        exc.user_message,
                    )
                elif exc.code not in RESTORE_USER_ERROR_CODES:
                    self.health.record_error(
                        exc,
                        snapshot.plan_id,
                        snapshot.id,
                    )

            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except Exception:
            logging.exception("Restore failure")
            error = AppError(
                "RESTORE_DESTINATION_FAILED",
                "Could not restore the version.",
                traceback.format_exc(),
            )
            self.repo.operation_state(
                operation_id,
                "failed",
                error,
            )

            if snapshot is not None:
                self.health.record_error(
                    error,
                    snapshot.plan_id,
                    snapshot.id,
                )

            return OperationResult(
                operation_id,
                False,
                error.user_message,
                error.code,
            )

        finally:
            if temporary and temporary.exists():
                if temporary.is_dir():
                    shutil.rmtree(
                        temporary,
                        ignore_errors=True,
                    )
                else:
                    temporary.unlink(missing_ok=True)

    def replace_original_file(
        self,
        operation_id: str,
        snapshot_id: str,
        progress: Callable[[ProgressEvent], None],
        cancelled: Callable[[], bool],
    ) -> OperationResult:
        snapshot: SnapshotInfo | None = None
        staged: Path | None = None
        old_copy: Path | None = None
        published = False

        try:
            self.repo.operation_state(operation_id, "running")
            snapshot = self.repo.snapshot(snapshot_id)

            if snapshot.state != "committed":
                raise AppError(
                    "SNAPSHOT_NOT_COMMITTED",
                    "The original file can be replaced only from an available version.",
                )
            if snapshot.source_kind != SourceKind.FILE.value:
                raise AppError(
                    "RESTORE_REPLACE_INVALID",
                    "Replacing a folder is not supported for this version.",
                )
            if snapshot.storage_mode not in {
                StorageMode.DIRECT.value,
                StorageMode.ZIP.value,
            }:
                raise AppError(
                    "STORAGE_MODE_INVALID",
                    "The version storage mode is not supported.",
                )

            source = Path(snapshot.source_path)
            PathPolicy._reject_special(str(source))

            if (
                not source.is_absolute()
                or source.is_symlink()
                or not source.is_file()
                or getattr(source, "is_junction", lambda: False)()
            ):
                raise AppError(
                    "RESTORE_REPLACE_INVALID",
                    "The original file is missing or is not a regular file.",
                )

            parent = source.parent.resolve(strict=True)
            if parent != source.parent:
                raise AppError(
                    "RESTORE_REPLACE_INVALID",
                    "The path to the original file is redirected.",
                )

            if PathPolicy.inside(source, Path(snapshot.storage_path)):
                raise AppError(
                    "RESTORE_REPLACE_INVALID",
                    "The original file is inside the storage.",
                )

            if cancelled():
                raise CancelledError()

            paths = self.verifier.artifact_paths(snapshot)
            manifest = self.verifier._read_manifest(
                snapshot,
                paths.manifest_path,
                cancelled,
            )
            entries = manifest["entries"]
            files = [
                entry for entry in entries
                if entry.get("entry_type") == "file"
            ]

            if (
                len(files) != 1
                or manifest.get("source_kind") != SourceKind.FILE.value
            ):
                raise AppError(
                    "SNAPSHOT_CORRUPTED",
                    "The version content does not match the original file.",
                )

            saved_name = self._saved_file_name(entries)
            entry = files[0]
            expected_size = entry.get("size_bytes")
            expected_hash = entry.get("sha256")
            if (
                type(expected_size) is not int
                or expected_size < 0
                or not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or any(c not in "0123456789abcdef" for c in expected_hash)
            ):
                raise AppError(
                    "SNAPSHOT_CORRUPTED",
                    "The manifest contains invalid file information.",
                )

            staged = parent / f".amvibackup-replace-{operation_id}.tmp"
            if staged.exists() or staged.is_symlink():
                raise AppError(
                    "RESTORE_TEMP_EXISTS",
                    "The temporary replacement file already exists.",
                )

            if snapshot.storage_mode == StorageMode.DIRECT.value:
                backup_file = PathPolicy.safe_join_under(
                    paths.direct_data_root,
                    saved_name,
                )
                if not backup_file.is_file() or backup_file.is_symlink():
                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "The saved version file is missing.",
                    )

                with (
                    backup_file.open("rb") as input_stream,
                    staged.open("xb") as output_stream,
                ):
                    while True:
                        if cancelled():
                            raise CancelledError()
                        chunk = input_stream.read(COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        output_stream.write(chunk)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            else:
                expected_files = self.verifier.zip_reader._manifest_files(entries)
                expected_dirs = self.verifier.zip_reader._manifest_directories(entries)
                members = self.verifier.zip_reader._archive_members(
                    paths.zip_artifact,
                    expected_files,
                    expected_dirs,
                    cancelled,
                )
                info = members.get(saved_name)
                if info is None or info.file_size != expected_size:
                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "The ZIP file does not match the manifest.",
                    )

                with (
                    zipfile.ZipFile(paths.zip_artifact, "r") as archive,
                    archive.open(info, "r") as input_stream,
                    staged.open("xb") as output_stream,
                ):
                    while True:
                        if cancelled():
                            raise CancelledError()
                        chunk = input_stream.read(COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        output_stream.write(chunk)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())

            self._verify_restored_output(
                staged,
                SourceKind.FILE.value,
                [entry],
                cancelled,
            )
            progress(
                ProgressEvent(
                    operation_id,
                    "Checking the file before replacing…",
                    str(source),
                    1,
                    1,
                    expected_size,
                    expected_size,
                )
            )

            if cancelled():
                raise CancelledError()

            before = source.stat()
            old_hash = sha256_file(source, cancelled)

            old_copy = (
                parent
                / f"{source.stem}-before-restore-{operation_id}{source.suffix}"
            )
            if old_copy.exists() or old_copy.is_symlink():
                raise AppError(
                    "RESTORE_PREREPLACE_FAILED",
                    "The name for the copy of the current file is already taken.",
                )

            try:
                with (
                    source.open("rb") as input_stream,
                    old_copy.open("xb") as output_stream,
                ):
                    while True:
                        if cancelled():
                            raise CancelledError()
                        chunk = input_stream.read(COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        output_stream.write(chunk)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())

                after = source.stat()
                if (
                    before.st_dev != after.st_dev
                    or before.st_ino != after.st_ino
                    or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or sha256_file(source, cancelled) != old_hash
                    or sha256_file(old_copy, cancelled) != old_hash
                ):
                    raise AppError(
                        "RESTORE_SOURCE_CHANGED",
                        "The original file changed while the replacement was being prepared.",
                    )
            except CancelledError:
                if old_copy is not None:
                    old_copy.unlink(missing_ok=True)
                old_copy = None
                raise
            except AppError:
                if old_copy is not None:
                    old_copy.unlink(missing_ok=True)
                old_copy = None
                raise
            except OSError as exc:
                if old_copy is not None:
                    old_copy.unlink(missing_ok=True)
                old_copy = None
                raise AppError(
                    "RESTORE_PREREPLACE_FAILED",
                    "Could not save a copy of the current file. Replacement cancelled.",
                    str(exc),
                ) from exc

            if cancelled():
                raise CancelledError()

            # On replacement the original is checked once more. The copy of the old
            # content stays in place even if a later step fails.
            current = source.stat()
            if (
                source.is_symlink()
                or not source.is_file()
                or current.st_dev != before.st_dev
                or current.st_ino != before.st_ino
                or current.st_size != before.st_size
                or current.st_mtime_ns != before.st_mtime_ns
                or sha256_file(source, cancelled) != old_hash
            ):
                raise AppError(
                    "RESTORE_SOURCE_CHANGED",
                    "The original file changed before the replacement.",
                )

            self.repo.recovery_checkpoint(operation_id, {
                "stage": "ready_to_publish", "snapshot_id": snapshot.id,
                "source_path": str(source), "previous_copy_path": str(old_copy),
                "previous_sha256": old_hash, "restored_sha256": expected_hash,
            })
            # Publication boundary: a cancel after it no longer restores the
            # previous state; the saved copy stays available.
            os.replace(staged, source)
            published = True
            staged = None

            self._verify_restored_output(
                source,
                SourceKind.FILE.value,
                [entry],
                lambda: False,
            )
            self.repo.adopt_restored_source_identity(snapshot.plan_id)
            self.repo.operation_state(operation_id, "succeeded")
            self.repo.activity(
                "info",
                "RESTORE_REPLACE_SUCCEEDED",
                "The original file was replaced with a saved version.",
                snapshot.plan_id,
                operation_id,
                {
                    "snapshot_id": snapshot.id,
                    "source_path": str(source),
                    "previous_copy_path": str(old_copy),
                },
            )
            return OperationResult(
                operation_id,
                True,
                "The original file was restored.",
                result_code="RESTORE_REPLACE_SUCCEEDED",
                destination_path=str(old_copy),
            )

        except CancelledError as exc:
            self.repo.operation_state(operation_id, "cancelled", exc)
            return OperationResult(
                operation_id, False, exc.user_message, exc.code
            )

        except AppError as exc:
            if published:
                exc = AppError(
                    "RESTORE_REPLACE_REVIEW_REQUIRED",
                    "The file may have been replaced, but the final check failed. "
                    "Check the original file and the saved copy.",
                    exc.technical_details,
                )
            self.repo.operation_state(operation_id, "failed", exc)
            if snapshot is not None:
                self.health.record_error(
                    exc, snapshot.plan_id, snapshot.id
                )
            return OperationResult(
                operation_id, False, exc.user_message, exc.code
            )

        except Exception:
            logging.exception("Replace-original restore failure")
            error = AppError(
                "RESTORE_REPLACE_REVIEW_REQUIRED"
                if published
                else "RESTORE_DESTINATION_FAILED",
                "Check the original file and its saved copy."
                if published
                else "Could not replace the original file.",
                traceback.format_exc(),
            )
            self.repo.operation_state(operation_id, "failed", error)
            if snapshot is not None:
                self.health.record_error(
                    error, snapshot.plan_id, snapshot.id
                )
            return OperationResult(
                operation_id, False, error.user_message, error.code
            )

        finally:
            if staged is not None and staged.exists():
                staged.unlink(missing_ok=True)

    def restore_to_directory(
        self,
        operation_id: str,
        snapshot_id: str,
        directory_text: str,
        progress: Callable[[ProgressEvent], None],
        cancelled: Callable[[], bool],
    ) -> OperationResult:
        snapshot: SnapshotInfo | None = None
        temporary: Path | None = None

        try:
            self.repo.operation_state(operation_id, "running")
            snapshot = self.repo.snapshot(snapshot_id)

            if snapshot.state != "committed":
                raise AppError(
                    "SNAPSHOT_NOT_COMMITTED",
                    "You can restore only "
                    "an available version.",
                )

            if snapshot.storage_mode not in {
                StorageMode.DIRECT.value,
                StorageMode.ZIP.value,
            }:
                raise AppError(
                    "STORAGE_MODE_INVALID",
                    "The version mode is not supported.",
                )

            if str(uuid.UUID(operation_id)) != operation_id:
                raise AppError(
                    "OPERATION_PAYLOAD_INVALID",
                    "Invalid operation identifier.",
                )

            selected = self._validate_selected_destination(
                snapshot,
                directory_text,
            )

            if cancelled():
                raise CancelledError()

            paths = self.verifier.artifact_paths(snapshot)
            manifest = self.verifier._read_manifest(
                snapshot,
                paths.manifest_path,
                cancelled,
            )
            entries = manifest["entries"]
            files = [
                entry
                for entry in entries
                if entry.get("entry_type") == "file"
            ]

            if snapshot.source_kind == SourceKind.FILE.value:
                if (
                    len(files) != 1
                    or any(
                        entry.get("entry_type") == "directory"
                        for entry in entries
                    )
                ):
                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "The file version has an invalid structure.",
                    )

                name = str(files[0]["relative_path"])
                if (
                    Path(name).name != name
                    or name in {"", ".", ".."}
                ):
                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "The file name in the version is unsafe.",
                    )
            elif snapshot.source_kind == SourceKind.FOLDER.value:
                name = Path(snapshot.source_path).name
                if not name or name in {".", ".."}:
                    raise AppError(
                        "RESTORE_DESTINATION_INVALID",
                        "The source folder name is unavailable.",
                    )
            else:
                raise AppError(
                    "SNAPSHOT_CORRUPTED",
                    "The source type in the version is unknown.",
                )

            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            is_file_version = snapshot.source_kind == SourceKind.FILE.value
            final = unique_restored_path(
                selected,
                name,
                is_file_version,
                stamp,
                force_suffix=True,
            )
            temporary = (
                selected
                / f".amvibackup-restore-{operation_id}.tmp"
            )

            if temporary.exists() or temporary.is_symlink():
                raise AppError(
                    "RESTORE_TEMP_EXISTS",
                    "The temporary restore path "
                    "already exists.",
                )

            if (
                snapshot.storage_mode == StorageMode.DIRECT.value
                and not paths.direct_data_root.is_dir()
            ):
                raise AppError(
                    "SNAPSHOT_CORRUPTED",
                    "The saved version data is missing.",
                )

            temporary.mkdir(exist_ok=False)

            if snapshot.storage_mode == StorageMode.DIRECT.value:
                for entry in entries:
                    if cancelled():
                        raise CancelledError()

                    relative = str(entry["relative_path"])
                    destination = PathPolicy.safe_join_under(
                        temporary,
                        relative,
                    )

                    if entry["entry_type"] == "directory":
                        destination.mkdir(
                            parents=True,
                            exist_ok=True,
                        )
                        continue

                    if entry["entry_type"] != "file":
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            "The manifest contains an unknown entry.",
                        )

                    original = PathPolicy.safe_join_under(
                        paths.direct_data_root,
                        relative,
                    )
                    if not original.is_file():
                        raise AppError(
                            "SNAPSHOT_CORRUPTED",
                            "The saved version file is missing.",
                        )

                    destination.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    with (
                        original.open("rb") as input_stream,
                        destination.open("xb") as output_stream,
                    ):
                        while True:
                            if cancelled():
                                raise CancelledError()

                            chunk = input_stream.read(
                                COPY_CHUNK_SIZE
                            )
                            if not chunk:
                                break

                            output_stream.write(chunk)

                    shutil.copystat(
                        original,
                        destination,
                        follow_symlinks=False,
                    )
            else:
                self.verifier.zip_reader.extract_to(
                    paths.zip_artifact,
                    entries,
                    temporary,
                    cancelled,
                    progress,
                    operation_id,
                )

            if snapshot.source_kind == SourceKind.FILE.value:
                staged = PathPolicy.safe_join_under(
                    temporary,
                    name,
                )
                self._verify_restored_output(
                    staged,
                    snapshot.source_kind,
                    entries,
                    cancelled,
                )
            else:
                staged = temporary
                self._verify_restored_output(
                    staged,
                    snapshot.source_kind,
                    entries,
                    cancelled,
                )

            if cancelled():
                raise CancelledError()

            set_writable_best_effort(staged)
            self._validate_selected_destination(snapshot, directory_text)

            if final.exists() or final.is_symlink():
                # Someone created the same name while we were copying: pick another free name, never overwrite.
                final = unique_restored_path(
                    selected,
                    name,
                    is_file_version,
                    stamp,
                    force_suffix=True,
                )

            if snapshot.source_kind == SourceKind.FILE.value:
                move_restore_no_replace(staged, final)
                temp_directory = temporary
                temporary = None

                try:
                    temp_directory.rmdir()
                except OSError:
                    logging.warning(
                        "Cannot remove restore staging directory: %s",
                        temp_directory,
                        exc_info=True,
                    )
            else:
                move_restore_no_replace(temporary, final)
                temporary = None

            self.repo.operation_state(operation_id, "succeeded")
            self.repo.activity(
                "info",
                "RESTORE_TO_DIRECTORY_SUCCEEDED",
                f"Version restored: {final}",
                snapshot.plan_id,
                operation_id,
            )

            return OperationResult(
                operation_id,
                True,
                f"Restored: {final}",
                result_code="RESTORE_TO_DIRECTORY_SUCCEEDED",
                destination_path=str(final),
            )

        except CancelledError as exc:
            self.repo.operation_state(
                operation_id,
                "cancelled",
                exc,
            )
            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except AppError as exc:
            self.repo.operation_state(
                operation_id,
                "failed",
                exc,
            )

            if snapshot is not None:
                if exc.code == "SNAPSHOT_CORRUPTED":
                    self.health.record_snapshot_corrupted(
                        snapshot.id,
                        snapshot.plan_id,
                        exc.user_message,
                    )
                elif exc.code not in RESTORE_USER_ERROR_CODES:
                    self.health.record_error(
                        exc,
                        snapshot.plan_id,
                        snapshot.id,
                    )

            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except Exception:
            logging.exception(
                "Restore to selected directory failed"
            )
            error = AppError(
                "RESTORE_DESTINATION_FAILED",
                "Could not restore the version "
                "to the selected folder.",
                traceback.format_exc(),
            )
            self.repo.operation_state(
                operation_id,
                "failed",
                error,
            )

            if snapshot is not None:
                self.health.record_error(
                    error,
                    snapshot.plan_id,
                    snapshot.id,
                )

            return OperationResult(
                operation_id,
                False,
                error.user_message,
                error.code,
            )

        finally:
            if (
                temporary is not None
                and temporary.is_dir()
                and not temporary.is_symlink()
                and not getattr(
                    temporary,
                    "is_junction",
                    lambda: False,
                )()
            ):
                shutil.rmtree(
                    temporary,
                    ignore_errors=True,
                )

    def restore_selected_files(
        self,
        operation_id: str,
        snapshot_id: str,
        selected_relative_paths: list[str],
        destination_text: str,
        progress: Callable[[ProgressEvent], None],
        cancelled: Callable[[], bool],
    ) -> OperationResult:
        snapshot: SnapshotInfo | None = None
        temporary: Path | None = None

        try:
            self.repo.operation_state(operation_id, "running")
            snapshot = self.repo.snapshot(snapshot_id)

            if snapshot.state != "committed":
                raise AppError(
                    "SNAPSHOT_NOT_COMMITTED",
                    "You can restore only "
                    "an available version.",
                )

            if snapshot.source_kind != SourceKind.FOLDER.value:
                raise AppError(
                    "RESTORE_FOLDER_ONLY",
                    "Selective restore is possible "
                    "only for files from a saved folder.",
                )

            if snapshot.storage_mode not in {
                StorageMode.DIRECT.value,
                StorageMode.ZIP.value,
            }:
                raise AppError(
                    "STORAGE_MODE_INVALID",
                    "The version mode is not supported.",
                )

            if str(uuid.UUID(operation_id)) != operation_id:
                raise AppError(
                    "OPERATION_PAYLOAD_INVALID",
                    "Invalid operation identifier.",
                )

            destination = self._validate_selected_destination(
                snapshot,
                destination_text,
            )

            if cancelled():
                raise CancelledError()

            paths = self.verifier.artifact_paths(snapshot)
            manifest = self.verifier._read_manifest(
                snapshot,
                paths.manifest_path,
                cancelled,
            )
            entries = manifest["entries"]
            selected_entries = self._selected_file_entries(
                entries,
                selected_relative_paths,
            )

            for entry in selected_entries:
                checked_restore_target(
                    destination,
                    str(entry["relative_path"]),
                )

            temporary = (
                destination.parent
                / f".amvibackup-restore-selected-{operation_id}.tmp"
            )

            if (
                temporary.exists()
                or temporary.is_symlink()
                or getattr(
                    temporary,
                    "is_junction",
                    lambda: False,
                )()
            ):
                raise AppError(
                    "RESTORE_TEMP_EXISTS",
                    "The temporary restore path "
                    "already exists.",
                )

            temporary.mkdir(parents=True, exist_ok=False)

            if snapshot.storage_mode == StorageMode.DIRECT.value:
                if (
                    not paths.direct_data_root.is_dir()
                    or paths.direct_data_root.is_symlink()
                ):
                    raise AppError(
                        "SNAPSHOT_CORRUPTED",
                        "The saved version data is missing.",
                    )

                self._copy_selected_direct(
                    paths.direct_data_root,
                    selected_entries,
                    temporary,
                    operation_id,
                    cancelled,
                    progress,
                )
            else:
                self.verifier.zip_reader.extract_selected_to(
                    paths.zip_artifact,
                    entries,
                    [
                        str(entry["relative_path"])
                        for entry in selected_entries
                    ],
                    temporary,
                    cancelled,
                    progress,
                    operation_id,
                )

            self._verify_restored_output(
                temporary,
                SourceKind.FOLDER.value,
                selected_entries,
                cancelled,
            )

            if cancelled():
                raise CancelledError()

            publish_plan: list[tuple[Path, Path]] = []
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            reserved: set[Path] = set()

            for temporary_file in sorted(
                (
                    path
                    for path in temporary.rglob("*")
                    if path.is_file()
                ),
                key=lambda path: (len(path.parts), str(path).casefold()),
            ):
                relative_path = temporary_file.relative_to(
                    temporary
                ).as_posix()
                source_entry = next(
                    (
                        entry
                        for entry in selected_entries
                        if entry["relative_path"] == relative_path
                    ),
                    None,
                )

                if source_entry is None:
                    raise AppError(
                        "RESTORE_VERIFY_FAILED",
                        "The temporary restore contains "
                        "an unselected file.",
                    )

                wanted = checked_restore_target(
                    destination,
                    relative_path,
                )
                final_file = unique_restored_path(
                    wanted.parent,
                    wanted.name,
                    True,
                    stamp,
                    reserved,
                )
                reserved.add(final_file)
                publish_plan.append((temporary_file, final_file))

            for _temporary_file, final_file in publish_plan:
                final_file.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            for temporary_file, final_file in publish_plan:
                if cancelled():
                    raise CancelledError()

                if final_file.exists() or final_file.is_symlink():
                    # The name was taken a moment ago: use another free name, never overwrite.
                    final_file = unique_restored_path(
                        final_file.parent,
                        final_file.name,
                        True,
                        stamp,
                        reserved,
                    )
                    reserved.add(final_file)
                self._validate_selected_destination(snapshot, destination_text)
                checked_restore_target(destination, final_file.relative_to(destination).as_posix())
                set_writable_best_effort(temporary_file)
                move_restore_no_replace(temporary_file, final_file)

            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            temporary = None

            self.repo.operation_state(
                operation_id,
                "succeeded",
            )
            self.repo.activity(
                "info",
                "RESTORE_SELECTED_SUCCEEDED",
                "The selected files were restored.",
                snapshot.plan_id,
                operation_id,
                {
                    "snapshot_id": snapshot.id,
                    "selected_relative_paths": [
                        str(entry["relative_path"])
                        for entry in selected_entries
                    ],
                    "destination": str(destination),
                },
            )

            return OperationResult(
                operation_id,
                True,
                "The selected files were restored.",
                result_code="RESTORE_SELECTED_SUCCEEDED",
                destination_path=str(destination),
            )

        except CancelledError as exc:
            self.repo.operation_state(
                operation_id,
                "cancelled",
                exc,
            )
            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except AppError as exc:
            self.repo.operation_state(
                operation_id,
                "failed",
                exc,
            )

            if snapshot is not None:
                if exc.code == "SNAPSHOT_CORRUPTED":
                    self.health.record_snapshot_corrupted(
                        snapshot.id,
                        snapshot.plan_id,
                        exc.user_message,
                    )
                elif exc.code not in RESTORE_USER_ERROR_CODES:
                    self.health.record_error(
                        exc,
                        snapshot.plan_id,
                        snapshot.id,
                    )

            return OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except Exception:
            logging.exception(
                "Selected-file restore failure"
            )
            error = AppError(
                "RESTORE_DESTINATION_FAILED",
                "Could not restore the selected files.",
                traceback.format_exc(),
            )
            self.repo.operation_state(
                operation_id,
                "failed",
                error,
            )

            if snapshot is not None:
                self.health.record_error(
                    error,
                    snapshot.plan_id,
                    snapshot.id,
                )

            return OperationResult(
                operation_id,
                False,
                error.user_message,
                error.code,
            )

        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)



class RetentionService:
    def __init__(self, repo: Repository, storage: StorageService, health: HealthService) -> None:
        self.repo = repo
        self.storage = storage
        self.health = health

    def delete_one(self, snapshot: SnapshotInfo) -> None:
        full = self.repo.snapshot(snapshot.id)
        if full.state != "committed" or full.pinned:
            raise AppError("SNAPSHOT_NOT_COMMITTED" if full.state != "committed" else "SNAPSHOT_PINNED",
                           "Only an unpinned available version can be deleted.")
        committed = self.repo.available_snapshots(full.plan_id)
        if not committed or full.id == max(committed, key=lambda item: item.sequence_no).id:
            raise AppError("SNAPSHOT_LAST_COMMITTED", "The last available version of a plan cannot be deleted.")
        plan = self.repo.plan_details(full.plan_id)
        storage_root = Path(full.storage_path)
        snapshot_root = self.storage.safe_snapshot_root(storage_root, plan, full.sequence_no)
        if not snapshot_root.is_dir():
            raise AppError("SNAPSHOT_DELETE_FAILED", "The version folder was not found during deletion.")
        if full.storage_mode == StorageMode.DIRECT.value:
            expected_artifact = snapshot_root / StorageService.DIRECT_DATA_DIR_NAME
        elif full.storage_mode == StorageMode.ZIP.value:
            expected_artifact = snapshot_root / StorageService.ZIP_ARTIFACT_NAME
        else:
            raise AppError("STORAGE_MODE_INVALID", f"Unsupported storage mode: {full.storage_mode}")
        if not expected_artifact.exists() or not (snapshot_root / StorageService.MANIFEST_NAME).is_file()                 or not (snapshot_root / StorageService.SNAPSHOT_METADATA_NAME).is_file():
            raise AppError("SNAPSHOT_DELETE_FAILED", "The version data or metadata is missing from the storage.")
        self.repo.mark_snapshot(full.id, "deleting")
        try:
            self.storage.prepare_snapshot_for_deletion(storage_root, plan, full.sequence_no)
            shutil.rmtree(snapshot_root)
            self.repo.delete_record(full.id)
        except Exception as exc:
            detail = f"Deletion of version #{full.sequence_no} was interrupted; the data state needs a check."
            try: self.repo.mark_delete_needs_recovery(full.id, detail)
            except Exception: logging.exception("Cannot persist deletion recovery state for %s", full.id)
            try: self.health.record_error(AppError("OPERATION_NEEDS_RECOVERY",detail,str(exc)),full.plan_id)
            except Exception: logging.exception("Cannot record deletion issue for %s", full.id)
            raise
        self.repo.activity("info","SNAPSHOT_DELETED",f"Version #{full.sequence_no} deleted.",full.plan_id)

    def preview(self, plan_id: str, policy: RetentionPolicy | None = None,
                now_utc_ms: int | None = None) -> RetentionPreview:
        return self.repo.retention_preview(plan_id, policy, now_utc_ms)

    def apply(self, plan_id: str, expected_token: str | None = None,
              progress: Callable[[ProgressEvent], None] | None = None,
              cancelled: Callable[[], bool] | None = None,
              operation_id: str = "") -> RetentionPreview:
        plan = self.repo.plan_details(plan_id)
        policy = RetentionPolicy(plan.retention_mode,plan.keep_last_n,plan.retention_daily,
                                 plan.retention_weekly,plan.retention_monthly)
        preview = self.preview(plan_id, policy)
        if expected_token is not None and preview.token != expected_token:
            raise AppError("RETENTION_PREVIEW_STALE","The history changed. Review the deletion list again.")
        if policy.mode == RETENTION_MODE_TIME and preview.clock_blocked:
            error=AppError("RETENTION_CLOCK_REVIEW_REQUIRED",
                           "Time-based cleanup is paused: the system time changed. Check the date and the policy.")
            self.health.record_error(error,plan_id)
            self.repo.activity("warning",error.code,error.user_message,plan_id)
            return preview
        total=len(preview.delete_snapshot_ids)
        for index,snapshot_id in enumerate(preview.delete_snapshot_ids,1):
            if cancelled and cancelled(): raise CancelledError()
            snapshot=self.repo.snapshot(snapshot_id)
            self.delete_one(snapshot)
            if progress: progress(ProgressEvent(operation_id,"Cleaning up old versions…",f"Version #{snapshot.sequence_no}",index,total))
        self.repo.resolve_plan_issue(plan_id,"RETENTION_BLOCKED_BY_PINS")
        self.repo.resolve_plan_issue(plan_id,"RETENTION_CLOCK_REVIEW_REQUIRED")
        return preview




class DeleteService:
    def __init__(self, repo: Repository, storage: StorageService, health: HealthService) -> None:
        self.repo = repo
        self.storage = storage
        self.health = health

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return True
        if getattr(info, "st_file_attributes", 0) & 0x400:
            return True
        return bool(getattr(path, "is_junction", lambda: False)())

    @staticmethod
    def _clear_readonly(path: Path) -> None:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass

    def _protected_directories(self) -> list[Path]:
        home = Path.home()
        values = [
            os.environ.get("SystemRoot"),
            os.environ.get("WINDIR"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramW6432"),
            os.environ.get("USERPROFILE"),
            str(home),
        ]
        found = [Path(value) for value in values if value]
        for name in ("Desktop", "Documents", "Рабочий стол", "Документы"):
            found.append(home / name)
            found.append(home / "OneDrive" / name)
        public = os.environ.get("PUBLIC")
        if public:
            found.append(Path(public) / "Desktop")
            found.append(Path(public) / "Documents")
        return found

    def _is_forbidden_root(self, path: Path) -> bool:
        try:
            absolute = Path(os.path.abspath(path))
        except OSError:
            return True
        if absolute.parent == absolute:
            return True
        current = os.path.normcase(str(absolute))
        for protected in self._protected_directories():
            try:
                if os.path.normcase(os.path.abspath(protected)) == current:
                    return True
            except OSError:
                continue
        return False

    def _is_system_tree(self, path: Path) -> bool:
        try:
            absolute = os.path.normcase(os.path.abspath(path))
        except OSError:
            return True
        roots: list[str] = []
        for key in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            value = os.environ.get(key)
            if not value:
                continue
            try:
                roots.append(os.path.normcase(os.path.abspath(value)))
            except OSError:
                continue
        return any(absolute == root or absolute.startswith(root + os.sep) for root in roots)

    def _deletion_boundary_error(self, plan: PlanDetails, root: Path) -> str | None:
        storage = Path(plan.storage_path)
        if not str(plan.storage_path).strip() or not str(root).strip():
            return "empty"
        if not root.is_absolute() or not storage.is_absolute():
            return "relative"
        if self._is_reparse(storage):
            return "storage"
        try:
            storage_abs = Path(os.path.abspath(storage))
            root_abs = Path(os.path.abspath(root))
        except OSError:
            return "normalize"
        if root_abs == storage_abs or root_abs.parent != storage_abs:
            return "storage-root"
        if self._is_forbidden_root(root_abs) or self._is_system_tree(root_abs):
            return "protected"
        suffix = f"__job-{plan.id[:8]}".casefold()
        if not root_abs.name.casefold().endswith(suffix):
            return "name"
        if not self._is_reparse(root):
            try:
                resolved = root.resolve(strict=False)
                storage_resolved = storage.resolve(strict=False)
            except OSError:
                return "normalize"
            if resolved == storage_resolved or not PathPolicy.inside(resolved, storage_resolved):
                return "outside"
            if resolved.parent != storage_resolved:
                return "outside"
        return None

    def _validate_delete_storage(self, plan: PlanDetails) -> tuple[Path, list[str]]:
        storage = Path(plan.storage_path)
        warnings: list[str] = []
        if not str(plan.storage_path).strip() or not storage.is_absolute():
            raise AppError("PLAN_DELETE_UNOWNED_DATA", "The storage path is empty or relative.")
        if self._is_reparse(storage):
            raise AppError("PLAN_DELETE_UNOWNED_DATA", "The storage is a redirect (link or junction).")
        if not storage.is_dir():
            raise AppError("STORAGE_NOT_FOUND", "Connect the original storage before deleting.")
        marker = storage / "_amvibackup" / "storage.json"
        managed = storage / "_amvibackup"
        if self._is_reparse(managed) or self._is_reparse(marker):
            warnings.append("storage_marker")
            return storage, warnings
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            warnings.append("storage_marker")
            return storage, warnings
        if (not isinstance(data, dict) or data.get("application") != APP_NAME
                or data.get("storage_id") != plan.storage_root_id
                or data.get("storage_root_path") != str(storage)):
            warnings.append("storage_marker")
        return storage, warnings

    def _candidate_status(self, candidate: Path, plan: PlanDetails) -> str:
        if self._is_reparse(candidate):
            return "link"
        marker = candidate / "plan.json"
        if marker.exists():
            if self._is_reparse(marker):
                return "damaged"
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return "damaged"
            if isinstance(data, dict) and data.get("plan_id") == plan.id:
                return "owned"
            if isinstance(data, dict) and data.get("plan_id"):
                return "foreign"
            return "damaged"
        snapshots = candidate / "snapshots"
        if not snapshots.is_dir() or self._is_reparse(snapshots):
            return "unknown"
        saw_ours = False
        saw_foreign = False
        try:
            versions = list(snapshots.iterdir())
        except OSError:
            return "damaged"
        for version in versions:
            if self._is_reparse(version) or not version.is_dir():
                continue
            meta = version / StorageService.SNAPSHOT_METADATA_NAME
            if not meta.is_file() or self._is_reparse(meta):
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("plan_id") == plan.id:
                saw_ours = True
            elif isinstance(data, dict) and data.get("plan_id"):
                saw_foreign = True
        if saw_ours:
            return "owned"
        if saw_foreign:
            return "foreign"
        return "damaged"

    def _select_plan_directory(self, storage: Path, plan: PlanDetails) -> tuple[Path | None, bool]:
        suffix = f"__job-{plan.id[:8]}".casefold()
        default = storage / f"{sanitize_name(plan.display_name)}__job-{plan.id[:8]}"
        matches: list[Path] = []
        try:
            children = list(storage.iterdir())
        except OSError as exc:
            raise AppError("PLAN_DELETE_UNOWNED_DATA", "Could not read the storage.", str(exc)) from exc
        for child in children:
            if child.name.casefold().endswith(suffix):
                matches.append(child)
        owned = [item for item in matches if self._candidate_status(item, plan) == "owned"]
        damaged = [item for item in matches if self._candidate_status(item, plan) in {"damaged", "link"}]
        if len(owned) == 1:
            return owned[0], False
        if len(owned) > 1 or len(damaged) > 1:
            return None, True
        if len(damaged) == 1:
            return damaged[0], False
        if default.exists() or default.is_symlink():
            status = self._candidate_status(default, plan)
            if status in {"owned", "damaged", "link"}:
                return default, False
            return None, True
        return default, False

    def _scan_plan_tree(self, root: Path, cancelled: Callable[[], bool] | None,
                        warnings: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        pending = [root]
        while pending:
            if cancelled and cancelled():
                raise CancelledError()
            current = pending.pop()
            try:
                children = list(os.scandir(current))
            except OSError:
                if "scan_incomplete" not in warnings:
                    warnings.append("scan_incomplete")
                continue
            for entry in children:
                if cancelled and cancelled():
                    raise CancelledError()
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                try:
                    info = path.lstat()
                except OSError:
                    if "scan_incomplete" not in warnings:
                        warnings.append("scan_incomplete")
                    continue
                link = self._is_reparse(path)
                is_dir = stat.S_ISDIR(info.st_mode) and not link
                if link and "symlink" not in warnings:
                    warnings.append("symlink")
                if not link and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    if "unknown_files" not in warnings:
                        warnings.append("unknown_files")
                result[relative] = {
                    "directory": is_dir,
                    "link": link,
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
                if is_dir:
                    pending.append(path)
        return result

    def _note_tree_problems(self, tree: dict[str, dict[str, Any]], warnings: list[str]) -> None:
        for name in tree:
            parts = name.split("/")
            if parts[0] not in {"plan.json", "snapshots"}:
                if "unknown_files" not in warnings:
                    warnings.append("unknown_files")
                return
            if len(parts) >= 2 and not re.fullmatch(r"v\d{6}", parts[1]):
                if "unknown_files" not in warnings:
                    warnings.append("unknown_files")
                return

    def _note_metadata_problems(self, plan: PlanDetails, root: Path, warnings: list[str]) -> None:
        for snapshot in self.repo.snapshots(plan.id, True):
            if snapshot.state == "deleted":
                continue
            version = root / "snapshots" / f"v{snapshot.sequence_no:06d}"
            metadata_path = version / StorageService.SNAPSHOT_METADATA_NAME
            manifest_path = version / StorageService.MANIFEST_NAME
            if not version.exists():
                continue
            try:
                if self._is_reparse(metadata_path) or self._is_reparse(manifest_path):
                    raise ValueError("reparse metadata")
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mismatched = (
                    not isinstance(metadata, dict) or not isinstance(manifest, dict)
                    or metadata.get("plan_id") != plan.id or metadata.get("snapshot_id") != snapshot.id
                    or metadata.get("sequence_no") != snapshot.sequence_no
                    or manifest.get("snapshot_id") != snapshot.id
                    or metadata.get("storage_mode") != snapshot.storage_mode
                    or manifest.get("storage_mode") != snapshot.storage_mode
                    or sha256_file(manifest_path) != snapshot.manifest_sha256
                )
                entries = manifest.get("entries") if isinstance(manifest, dict) else None
                unsafe = snapshot.storage_mode == StorageMode.DIRECT.value and not isinstance(entries, list)
                if mismatched or unsafe:
                    raise ValueError("metadata mismatch")
            except (OSError, ValueError, TypeError):
                if "damaged_metadata" not in warnings:
                    warnings.append("damaged_metadata")
                return

    def _source_protection(self, plan: PlanDetails, root: Path) -> tuple[bool, list[str]]:
        source = Path(plan.source_path)
        if not str(plan.source_path).strip() or not source.is_absolute():
            return False, []
        try:
            source_abs = Path(os.path.abspath(source))
            root_abs = Path(os.path.abspath(root))
        except OSError:
            return False, []
        if source_abs == root_abs or PathPolicy.inside(root_abs, source_abs):
            return True, ["__versions_only__"]
        if PathPolicy.inside(source_abs, root_abs):
            relative = source_abs.relative_to(root_abs).as_posix()
            if relative == ".":
                return True, ["__versions_only__"]
            return True, [relative]
        return False, []

    @staticmethod
    def _is_protected(name: str, protected: list[str]) -> bool:
        if not protected:
            return False
        if protected == ["__versions_only__"]:
            return not (name == "plan.json" or name == "snapshots" or name.startswith("snapshots/"))
        return any(name == item or name.startswith(item + "/") for item in protected)

    def _empty_inventory(self, root: str, warnings: list[str], skip: bool) -> dict[str, Any]:
        problem_codes = {"damaged_metadata", "unknown_files", "symlink", "storage_marker", "scan_incomplete"}
        return {
            "root": root,
            "root_identity": None,
            "entries": {},
            "warnings": warnings,
            "skip_files": skip,
            "protected": [],
            "versions_only": False,
            "has_problems": any(item in problem_codes for item in warnings),
        }

    def _build_plan_delete_inventory(self, plan: PlanDetails, cancelled: Callable[[], bool]) -> dict[str, Any]:
        storage, warnings = self._validate_delete_storage(plan)
        if cancelled():
            raise CancelledError()
        selected, ambiguous = self._select_plan_directory(storage, plan)
        if ambiguous or selected is None:
            warnings.append("path_skipped")
            return self._empty_inventory("", warnings, True)
        boundary = self._deletion_boundary_error(plan, selected)
        if boundary is not None:
            warnings.append("path_skipped")
            return self._empty_inventory(str(selected), warnings, True)
        if not selected.exists() and not self._is_reparse(selected):
            return self._empty_inventory(str(selected), warnings, True)
        overlap, protected = self._source_protection(plan, selected)
        if overlap and "source_overlap" not in warnings:
            warnings.append("source_overlap")
        root_is_link = self._is_reparse(selected)
        tree = {} if root_is_link else self._scan_plan_tree(selected, cancelled, warnings)
        if not root_is_link:
            self._note_tree_problems(tree, warnings)
            self._note_metadata_problems(plan, selected, warnings)
        try:
            info = selected.lstat()
            identity = [info.st_dev, info.st_ino]
        except OSError:
            identity = None
        problem_codes = {"damaged_metadata", "unknown_files", "symlink", "storage_marker", "scan_incomplete"}
        return {
            "root": str(selected),
            "root_identity": identity,
            "entries": tree,
            "warnings": warnings,
            "skip_files": False,
            "protected": protected,
            "versions_only": protected == ["__versions_only__"],
            "root_is_link": root_is_link,
            "has_problems": any(item in problem_codes for item in warnings),
        }

    def _validate_plan_delete_inventory(self, plan: PlanDetails, inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
        root_text = str(inventory.get("root") or "")
        if inventory.get("skip_files") or not root_text:
            return {}
        root = Path(root_text)
        if self._deletion_boundary_error(plan, root) is not None:
            raise AppError("PLAN_DELETE_UNOWNED_DATA", "The deletion log contains an unsafe path.")
        storage, _warnings = self._validate_delete_storage(plan)
        if not PathPolicy.inside(root, storage) and not self._is_reparse(root):
            raise AppError("PLAN_DELETE_UNOWNED_DATA", "The deletion log contains an unsafe path.")
        if self._is_reparse(root):
            return {}
        warnings: list[str] = []
        return self._scan_plan_tree(root, None, warnings)

    def _checked_delete_target(self, inventory: dict[str, Any], name: str) -> Path | None:
        if inventory.get("skip_files"):
            return None
        root = Path(inventory["root"])
        if not root.exists() and not self._is_reparse(root):
            return None
        if self._is_protected(name, list(inventory.get("protected") or [])):
            return None
        target = root / Path(name)
        try:
            lexical = target.absolute().relative_to(root.absolute())
        except ValueError as exc:
            raise AppError("PLAN_DELETE_UNOWNED_DATA", "A deletion path points outside the plan folder.", str(exc)) from exc
        if ".." in lexical.parts:
            raise AppError("PLAN_DELETE_UNOWNED_DATA", "A deletion path points outside the plan folder.")
        current = root
        for part in Path(name).parts[:-1]:
            current = current / part
            if self._is_reparse(current):
                return None
            try:
                if not PathPolicy.inside(current, root):
                    raise AppError("PLAN_DELETE_UNOWNED_DATA", "A nested deletion path points outside the plan folder.")
            except OSError as exc:
                raise AppError("PLAN_DELETE_UNOWNED_DATA", "A nested deletion path points outside the plan folder.", str(exc)) from exc
        if not target.exists() and not self._is_reparse(target):
            return None
        return target

    def _remove_entry(self, path: Path) -> None:
        link = self._is_reparse(path)
        info = path.lstat()
        if link:
            if stat.S_ISDIR(info.st_mode):
                os.rmdir(path)
            else:
                os.unlink(path)
            return
        if stat.S_ISDIR(info.st_mode):
            os.rmdir(path)
        else:
            os.unlink(path)

    def _remove_with_retry(self, path: Path) -> bool:
        attempts = len(PLAN_DELETE_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                self._remove_entry(path)
                return True
            except FileNotFoundError:
                return True
            except OSError:
                self._clear_readonly(path)
                if attempt >= len(PLAN_DELETE_RETRY_DELAYS):
                    return False
                time.sleep(PLAN_DELETE_RETRY_DELAYS[attempt])
        return False

    def _erase_plan_tree(self, inventory: dict[str, Any],
                         progress: Callable[[ProgressEvent], None],
                         cancelled: Callable[[], bool],
                         operation_id: str) -> tuple[int, int, list[str]]:
        deleted = 0
        failed = 0
        leftovers: list[str] = []
        if inventory.get("skip_files") or not inventory.get("root"):
            return deleted, failed, leftovers
        root = Path(inventory["root"])
        if inventory.get("root_is_link"):
            if self._remove_with_retry(root):
                deleted += 1
            else:
                failed += 1
                leftovers.append(str(root))
            return deleted, failed, leftovers
        warnings: list[str] = []
        tree = self._scan_plan_tree(root, cancelled, warnings) if (root.exists() or self._is_reparse(root)) else {}
        inventory["entries"] = tree

        def remove_tree(entries: dict[str, dict[str, Any]]) -> None:
            nonlocal deleted, failed
            files = sorted(name for name, info in entries.items()
                           if not info["directory"] or info.get("link"))
            directories = sorted((name for name, info in entries.items() if info["directory"]),
                                 key=lambda name: (len(name.split("/")), name), reverse=True)
            order = files + directories
            total = max(1, len(order))
            for index, name in enumerate(order, 1):
                if cancelled():
                    raise CancelledError()
                try:
                    path = self._checked_delete_target(inventory, name)
                except AppError:
                    continue
                if path is None:
                    continue
                info = entries[name]
                removed = self._remove_with_retry(path)
                countable = (not info["directory"]) or info.get("link")
                if removed and countable:
                    deleted += 1
                elif not removed:
                    failed += 1
                    leftovers.append(str(path))
                progress(ProgressEvent(operation_id, "Deleting the plan and its data…", name, index, total))

        remove_tree(tree)
        if cancelled():
            raise CancelledError()
        if root.exists() and not self._is_reparse(root):
            fresh: list[str] = []
            extra = self._scan_plan_tree(root, cancelled, fresh)
            pending = {name: info for name, info in extra.items() if name not in tree}
            if pending:
                remove_tree(pending)
        if (not leftovers and not inventory.get("protected") and not inventory.get("versions_only")
                and (root.exists() or self._is_reparse(root))):
            if not self._remove_with_retry(root):
                failed += 1
                leftovers.append(str(root))
        return deleted, failed, leftovers

    def delete_plan_data(self, operation_id: str, plan_id: str, preview_token: str,
                         approve_pinned: bool, progress: Callable[[ProgressEvent], None],
                         cancelled: Callable[[], bool]) -> OperationResult:
        started = False
        try:
            self.repo.operation_state(operation_id, "running")
            plan = self.repo.plan_details(plan_id)
            preview = self.repo.plan_deletion_preview(plan_id)
            if preview.token != preview_token:
                raise AppError("PLAN_DELETE_CHANGED", "The plan changed after confirmation. Preview again.")
            if preview.pinned_count and not approve_pinned:
                raise AppError("SNAPSHOT_PINNED", "Pinned versions require a separate dangerous-action confirmation.")
            if any(row["id"] != operation_id for row in self.repo.unfinished_operations()):
                raise AppError("OPERATION_ALREADY_RUNNING", "Another operation is running for this plan.")
            if cancelled():
                raise CancelledError()
            self.repo.discard_plan_deletion_job(plan_id)
            try:
                inventory = self._build_plan_delete_inventory(plan, cancelled)
            except AppError as exc:
                if exc.code != "PLAN_DELETE_UNOWNED_DATA":
                    raise
                inventory = self._empty_inventory("", [exc.user_message, "path_skipped"], True)
            if cancelled():
                raise CancelledError()
            if self.repo.plan_deletion_preview(plan_id).token != preview_token:
                raise AppError("PLAN_DELETE_CHANGED", "The data set changed during the check.")
            self.repo.begin_plan_deletion(plan_id, operation_id, inventory, preview)
            started = True
            deleted, failed, leftovers = self._erase_plan_tree(inventory, progress, cancelled, operation_id)
            if cancelled():
                raise CancelledError()
            self.repo.finish_plan_deletion(plan_id, operation_id, deleted, failed)
            message = f"Deleted {deleted} files, failed {failed}."
            folder = str(inventory.get("root") or "")
            return OperationResult(
                operation_id, True, message, result_code="PLAN_DELETE_SUCCEEDED",
                result_data={"deleted_files": deleted, "failed_files": failed,
                             "leftovers": leftovers, "folder": folder},
            )
        except CancelledError as exc:
            self.repo.operation_state(operation_id, "cancelled", exc)
            return OperationResult(operation_id, False, exc.user_message, exc.code)
        except AppError as exc:
            self.repo.operation_state(operation_id, "failed", exc)
            return OperationResult(operation_id, False, exc.user_message, exc.code)
        except Exception:
            logging.exception("Plan deletion failed")
            error = AppError("PLAN_DELETE_FAILED", "Plan deletion was not performed.", traceback.format_exc())
            self.repo.operation_state(operation_id, "failed", error)
            if started:
                self.health.record_error(error, plan_id)
            return OperationResult(operation_id, False, error.user_message, error.code)

    def delete(
        self,
        operation_id: str,
        snapshot_ids: list[str],
        progress: Callable[[ProgressEvent], None],
        cancelled: Callable[[], bool],
    ) -> OperationResult:
        plan_id: str | None = None
        try:
            self.repo.operation_state(operation_id, "running")
            snapshots = [self.repo.snapshot(snapshot_id) for snapshot_id in snapshot_ids]
            if not snapshots:
                raise AppError("SNAPSHOT_NOT_FOUND", "No versions selected for deletion.")
            if any(item.plan_id != snapshots[0].plan_id for item in snapshots):
                raise AppError("PLAN_INVALID", "Versions of different plans cannot be deleted in one operation.")
            if any(item.state != "committed" for item in snapshots):
                raise AppError("SNAPSHOT_NOT_COMMITTED", "Only available versions can be deleted.")
            if any(item.pinned for item in snapshots):
                raise AppError("SNAPSHOT_PINNED", "Unpin the selected versions first.")

            plan_id = snapshots[0].plan_id
            committed_count = len([item for item in self.repo.snapshots(plan_id) if item.state == "committed"])
            if committed_count - len(snapshots) < 1:
                raise AppError("SNAPSHOT_LAST_COMMITTED", "The only saved version of a plan cannot be deleted.")

            retention = RetentionService(self.repo, self.storage, self.health)
            for index, snapshot in enumerate(snapshots, 1):
                if cancelled():
                    raise CancelledError()
                retention.delete_one(snapshot)
                progress(ProgressEvent(operation_id, "Deleting versions…", f"Version #{snapshot.sequence_no}", index, len(snapshots)))
            self.repo.operation_state(operation_id, "succeeded")
            return OperationResult(
                operation_id,
                True,
                "The selected versions were deleted.",
                result_code="DELETE_SUCCEEDED",
            )
        except CancelledError as exc:
            self.repo.operation_state(operation_id, "cancelled", exc)
            return OperationResult(operation_id, False, exc.user_message, exc.code)
        except AppError as exc:
            self.repo.operation_state(operation_id, "failed", exc)
            self.health.record_error(exc, plan_id)
            return OperationResult(operation_id, False, exc.user_message, exc.code)
        except Exception:
            logging.exception("Delete failure")
            error = AppError("SNAPSHOT_DELETE_FAILED", "Could not delete the selected versions.", traceback.format_exc())
            self.repo.operation_state(operation_id, "failed", error)
            self.health.record_error(error, plan_id)
            return OperationResult(operation_id, False, error.user_message, error.code)


class RecoveryService:
    def __init__(
        self,
        repo: Repository,
        health: HealthService,
    ) -> None:
        self.repo = repo
        self.health = health


    def run(self) -> None:
        self.repo.discard_plan_deletion_jobs()
        for operation in self.repo.unfinished_operations():
            operation_id = operation["id"]
            plan_id, snapshot_id = self.repo.recovery_mark(operation_id)
            if plan_id:
                self.health.record_error(
                    AppError(
                        "OPERATION_NEEDS_RECOVERY",
                        "The previous operation was interrupted. The unfinished version was not added to the available ones.",
                    ),
                    plan_id,
                    snapshot_id,
                )
                logging.info(
                    "Recovery retained temporary artifacts for manual inspection: %s",
                    operation_id,
                )
        for snapshot in self.repo.unfinished_snapshots():
            detail = (
                f"Version #{snapshot.sequence_no} is not finished "
                "or was being deleted when the work was interrupted. "
                "Files were not changed; a check is required."
            )
            plan_id = self.repo.quarantine_unfinished_snapshot(snapshot.id, detail)
            if plan_id is not None:
                self.health.record_error(
                    AppError("OPERATION_NEEDS_RECOVERY", detail),
                    plan_id,
                    snapshot.id,
                )

    def _confirmed_rename_history(
        self,
        snapshot: SnapshotInfo,
        plan: PlanDetails,
        saved_name: str,
    ) -> bool:
        with self.repo.db.read_connection() as conn:
            rows = conn.execute(
                "SELECT details_json FROM ActivityLog "
                "WHERE plan_id=? AND event_code='SOURCE_RELOCATED' "
                "AND created_at_utc_ms>=? "
                "ORDER BY created_at_utc_ms,rowid",
                (plan.id, snapshot.created_at_utc_ms),
            ).fetchall()

        previous: str | None = None

        for row in rows:
            try:
                item = json.loads(row["details_json"])
                old_path = item["old_path"]
                new_path = item["new_path"]

                if (
                    not isinstance(old_path, str)
                    or not isinstance(new_path, str)
                ):
                    return False
            except (ValueError, TypeError, KeyError):
                return False

            if previous is None:
                if Path(old_path).name != saved_name:
                    return False
            elif old_path != previous:
                return False

            previous = new_path

        return previous == plan.source_path if previous is not None else False

    def _inspect_snapshot(
        self,
        snapshot: SnapshotInfo,
        operation_id: str,
        progress: Callable[[ProgressEvent], None],
        cancelled: Callable[[], bool],
    ) -> dict[str, Any] | None:
        plan = self.repo.plan_details(snapshot.plan_id)
        root = StorageService().safe_snapshot_root(
            Path(plan.storage_path), plan, snapshot.sequence_no
        )
        if cancelled():
            raise CancelledError()
        if not root.is_dir():
            return None

        try:
            metadata_path = root / StorageService.SNAPSHOT_METADATA_NAME
            manifest_path = root / StorageService.MANIFEST_NAME
            if metadata_path.is_symlink() or manifest_path.is_symlink():
                return None

            metadata = json.loads(
                metadata_path.read_text(encoding="utf-8")
            )
            manifest_hash = sha256_file(manifest_path, cancelled)
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            if not isinstance(metadata, dict) or not isinstance(manifest, dict):
                return None

            mode = snapshot.storage_mode
            expected_artifact = (
                StorageService.DIRECT_DATA_DIR_NAME
                if mode == StorageMode.DIRECT.value
                else StorageService.ZIP_ARTIFACT_NAME
                if mode == StorageMode.ZIP.value
                else None
            )
            if expected_artifact is None:
                return None

            if (
                metadata.get("format_version") != 1
                or metadata.get("snapshot_id") != snapshot.id
                or metadata.get("plan_id") != snapshot.plan_id
                or metadata.get("sequence_no") != snapshot.sequence_no
                or metadata.get("storage_mode") != mode
                or metadata.get("artifact_relpath") != expected_artifact
                or metadata.get("manifest_relpath")
                != StorageService.MANIFEST_NAME
                or metadata.get("manifest_sha256") != manifest_hash
                or manifest.get("format_version") != 1
                or manifest.get("snapshot_id") != snapshot.id
                or manifest.get("storage_mode") != mode
                or manifest.get("source_kind") != metadata.get("source_kind")
                or metadata.get("source_kind")
                not in {SourceKind.FILE.value, SourceKind.FOLDER.value}
            ):
                return None

            # A copy is not usable by the existing restore path if the
            # configured source changed from a file to a folder or vice versa.
            if metadata["source_kind"] != plan.source_kind:
                return None

            fingerprint = metadata.get("source_fingerprint")
            if (
                not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or any(c not in "0123456789abcdef" for c in fingerprint)
            ):
                return None

            entries = manifest.get("entries")
            if not isinstance(entries, list):
                return None

            paths_seen: set[str] = set()
            file_entries: list[dict[str, Any]] = []
            total_bytes = 0

            for entry in entries:
                if not isinstance(entry, dict):
                    return None

                relative = entry.get("relative_path")
                if not isinstance(relative, str):
                    return None

                safe_path = PathPolicy.safe_join_under(
                    (
                        root / StorageService.DIRECT_DATA_DIR_NAME
                        if mode == StorageMode.DIRECT.value
                        else root
                    ),
                    relative,
                )
                if relative in paths_seen:
                    return None
                paths_seen.add(relative)

                kind = entry.get("entry_type")
                if kind == "directory":
                    if mode == StorageMode.DIRECT.value and not safe_path.is_dir():
                        return None
                elif kind == "file":
                    digest = entry.get("sha256")
                    size = entry.get("size_bytes")
                    if (
                        not isinstance(digest, str)
                        or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest)
                        or type(size) is not int
                        or size < 0
                    ):
                        return None
                    total_bytes += size
                    file_entries.append(entry)
                else:
                    return None

            if metadata["source_kind"] == SourceKind.FILE.value:
                if len(file_entries) != 1:
                    return None

                saved = file_entries[0]["relative_path"]
                if (
                    not isinstance(saved, str)
                    or not saved
                    or saved in {".", ".."}
                    or "/" in saved
                    or chr(92) in saved
                    or ":" in saved
                ):
                    return None

                if (
                    saved != Path(plan.source_path).name
                    and not self._confirmed_rename_history(
                        snapshot,
                        plan,
                        saved,
                    )
                ):
                    return None

            count = metadata.get("file_count")
            logical = metadata.get("logical_size_bytes")
            stored = metadata.get("stored_size_bytes")
            if (
                type(count) is not int
                or count != len(file_entries)
                or type(logical) is not int
                or logical != total_bytes
                or type(stored) is not int
                or stored < 0
            ):
                return None

            if mode == StorageMode.DIRECT.value:
                artifact = root / StorageService.DIRECT_DATA_DIR_NAME
                if (
                    artifact.is_symlink()
                    or not artifact.is_dir()
                    or stored != total_bytes
                ):
                    return None

                for index, entry in enumerate(file_entries, 1):
                    if cancelled():
                        raise CancelledError()
                    target = PathPolicy.safe_join_under(
                        artifact, str(entry["relative_path"])
                    )
                    if (
                        not target.is_file()
                        or target.is_symlink()
                        or target.stat().st_size != entry["size_bytes"]
                        or sha256_file(target, cancelled) != entry["sha256"]
                    ):
                        return None
                    progress(
                        ProgressEvent(
                            operation_id,
                            "Verifying the version after a failure…",
                            str(target),
                            index,
                            count,
                        )
                    )
            else:
                artifact = root / StorageService.ZIP_ARTIFACT_NAME
                if (
                    artifact.is_symlink()
                    or not artifact.is_file()
                    or artifact.stat().st_size != stored
                ):
                    return None
                ZipArtifactReader().verify(
                    artifact, entries, cancelled, progress, operation_id
                )

            return {
                "logical_size_bytes": logical,
                "stored_size_bytes": stored,
                "file_count": count,
                "source_fingerprint": fingerprint,
                "manifest_sha256": manifest_hash,
            }
        except CancelledError:
            raise
        except (
            AppError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
        ):
            return None

    def _replace_review(self, row: sqlite3.Row, plan: PlanDetails) -> dict[str, Any]:
        source = Path(plan.source_path)
        op_id = row["id"]
        previous = source.parent / f"{source.stem}-before-restore-{op_id}{source.suffix}"
        staged = source.parent / f".amvibackup-replace-{op_id}.tmp"
        unresolved = {
            "state": "failed", "manual_review": True,
            "error_code": "RESTORE_REPLACE_REVIEW_REQUIRED",
            "detail": f"Interrupted replacement: check the original {source} and the previous copy {previous}. No files were deleted.",
        }
        try:
            cp = self.repo.operation_payload(op_id).get("recovery_checkpoint")
            if not isinstance(cp, dict) or cp.get("stage") != "ready_to_publish":
                return unresolved
            if (cp.get("source_path") != str(source) or cp.get("previous_copy_path") != str(previous)
                    or cp.get("snapshot_id") != row["snapshot_id"]):
                return unresolved
            before, restored = cp.get("previous_sha256"), cp.get("restored_sha256")
            if not all(isinstance(v, str) and len(v) == 64 and
                       all(c in "0123456789abcdef" for c in v) for v in (before, restored)):
                return unresolved
            if (source.is_symlink() or previous.is_symlink() or staged.is_symlink()
                    or not source.is_file() or not previous.is_file()
                    or sha256_file(previous) != before):
                return unresolved
            snapshot = self.repo.snapshot(row["snapshot_id"])
            if snapshot.state != "committed":
                return unresolved
            verifier = VerificationService(self.repo, StorageService(), self.health)
            manifest = verifier._read_manifest(snapshot, verifier.artifact_paths(snapshot).manifest_path, lambda: False)
            files = [item for item in manifest["entries"] if item.get("entry_type") == "file"]
            if len(files) != 1 or files[0].get("sha256") != restored:
                return unresolved

            saved = files[0].get("relative_path")
            if (
                not isinstance(saved, str)
                or not saved
                or saved in {".", ".."}
                or "/" in saved
                or chr(92) in saved
                or ":" in saved
            ):
                return unresolved

            if (
                saved != source.name
                and not self._confirmed_rename_history(
                    snapshot,
                    plan,
                    saved,
                )
            ):
                return unresolved
            if not staged.exists() and sha256_file(source) == restored:
                return {"state": "succeeded"}
            return unresolved
        except (AppError, OSError, ValueError, TypeError, KeyError):
            return unresolved

    def _cleanup_operation_temp(self, plan: PlanDetails, operation_id: str) -> None:
        try:
            if str(uuid.UUID(operation_id)) != operation_id:
                raise ValueError("operation id")
        except ValueError as exc:
            raise AppError("RECOVERY_CLEANUP_UNSAFE", "Invalid operation ID.", str(exc)) from exc
        storage = Path(plan.storage_path)
        managed = storage / "_amvibackup"
        parent = managed / "temporary"
        target = parent / operation_id
        if not target.exists() and not target.is_symlink():
            return
        for path in (storage, managed, parent, target):
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise AppError("RECOVERY_CLEANUP_UNSAFE", "The temporary path is redirected.")
        if not target.is_dir() or target.parent != parent or not PathPolicy.inside(target, parent):
            raise AppError("RECOVERY_CLEANUP_UNSAFE", "The temporary folder does not belong to the operation.")
        marker = managed / "storage.json"
        if not marker.is_file() or marker.is_symlink():
            raise AppError("RECOVERY_CLEANUP_UNSAFE", "The storage marker is missing.")
        try:
            identity = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AppError("RECOVERY_CLEANUP_UNSAFE", "Could not check the storage.", str(exc)) from exc
        if (not isinstance(identity, dict) or identity.get("storage_id") != plan.storage_root_id
                or identity.get("storage_root_path") != str(storage)):
            raise AppError("RECOVERY_CLEANUP_UNSAFE", "Ownership of the storage is not confirmed.")
        for directory, dirs, files in os.walk(target, followlinks=False):
            for name in dirs + files:
                candidate = Path(directory) / name
                if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
                    raise AppError("RECOVERY_CLEANUP_UNSAFE", "A redirect was found in the temporary folder.")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise AppError("RECOVERY_CLEANUP_FAILED", "Could not remove the temporary data.", str(exc)) from exc
        self.repo.activity("info", "RECOVERY_TEMP_CLEANED", "The temporary data of the operation was removed.",
                           plan.id, operation_id, {"temporary_path": str(target)})
    def review_plan(self, operation_id: str, plan_id: str,
                    progress: Callable[[ProgressEvent], None],
                    cancelled: Callable[[], bool]) -> OperationResult:
        try:
            self.repo.operation_state(operation_id, "running")
            plan = self.repo.plan_details(plan_id)
            if self.repo.plan_deletion_job(plan_id):
                self.repo.discard_plan_deletion_job(plan_id)
            storage = Path(plan.storage_path)
            if not storage.is_dir():
                raise AppError("RECOVERY_STORAGE_UNAVAILABLE", "The storage is unavailable for checking.")
            if not self.repo.needs_recovery_for_plan(plan_id):
                raise AppError("RECOVERY_NOT_REQUIRED", "The plan has no interrupted operations.")
            others = [item for item in self.repo.unfinished_operations_for_plan(plan_id) if item["id"] != operation_id]
            if others:
                raise AppError("OPERATION_ALREADY_RUNNING", "Another operation is still running for this plan.")
            with self.repo.db.read_connection() as conn:
                originals = conn.execute("SELECT * FROM Operations WHERE plan_id=? AND state='needs_recovery'",
                                         (plan_id,)).fetchall()
                delete_events = conn.execute("SELECT details_json FROM ActivityLog WHERE plan_id=? AND event_code='SNAPSHOT_DELETE_STARTED'",
                                             (plan_id,)).fetchall()
            deleting = set()
            for event in delete_events:
                try:
                    deleting.add(json.loads(event["details_json"])["snapshot_id"])
                except (ValueError, TypeError, KeyError):
                    continue
            reviews = {}
            for row in originals:
                if cancelled():
                    raise CancelledError()
                if row["type"] == OperationType.RESTORE_REPLACE_FILE.value:
                    reviews[row["id"]] = self._replace_review(row, plan)
                elif row["type"] in {OperationType.RESTORE_BESIDE.value,
                                      OperationType.RESTORE_TO_DIRECTORY.value,
                                      OperationType.RESTORE_SELECTED_FILES.value}:
                    reviews[row["id"]] = {
                        "state": "failed", "manual_review": True,
                        "error_code": "RESTORE_RECOVERY_REVIEW_REQUIRED",
                        "detail": "The restore was interrupted; check the files at the restore location. No files were deleted.",
                    }
                elif row["type"] == OperationType.DELETE_SNAPSHOTS.value:
                    try:
                        deleting.update(self.repo.operation_payload(row["id"]).get("snapshot_ids", []))
                    except AppError:
                        pass
            snapshots = [item for item in self.repo.unfinished_snapshots() if item.plan_id == plan_id]
            decisions = {}
            for snapshot in snapshots:
                if cancelled():
                    raise CancelledError()
                if snapshot.id in deleting:
                    root = StorageService().safe_snapshot_root(storage, plan, snapshot.sequence_no)
                    decisions[snapshot.id] = {"deleted": True} if not root.exists() else None
                else:
                    decisions[snapshot.id] = self._inspect_snapshot(snapshot, operation_id, progress, cancelled)
            if cancelled():
                raise CancelledError()
            for row in originals:
                if row["type"] == OperationType.BACKUP.value:
                    # Keep an unpublished copy if it might be the sole remaining data.
                    related = [x for x in snapshots if x.operation_id == row["id"]]
                    if (Path(plan.source_path).exists() or any(decisions[x.id] is not None for x in related)):
                        self._cleanup_operation_temp(plan, row["id"])
            accepted, unavailable = self.repo.finalize_recovery(plan_id, operation_id, decisions, reviews)
            return OperationResult(operation_id, True,
                f"Check finished: usable {accepted}, unavailable {unavailable}.",
                result_code="RECOVERY_FINISHED")
        except CancelledError as exc:
            self.repo.operation_state(operation_id, "cancelled", exc)
            return OperationResult(operation_id, False, exc.user_message, exc.code)
        except AppError as exc:
            self.repo.operation_state(operation_id, "failed", exc)
            return OperationResult(operation_id, False, exc.user_message, exc.code)
        except Exception:
            logging.exception("Recovery review failed")
            error = AppError("OPERATION_NEEDS_RECOVERY", "The check of the interrupted operation did not finish.", traceback.format_exc())
            self.repo.operation_state(operation_id, "failed", error)
            return OperationResult(operation_id, False, error.user_message, error.code)


class AutoBackupCoordinator:
    """Polling source watcher without copying, content hashing or a background worker.

    poll_once() is meant for deterministic tests and a future QTimer.
    It:
    - looks only at enabled plans with on_change_enabled;
    - stores a baseline on the first scan;
    - on a change updates the fingerprint and signals the coordinator;
    - creates no operation and does not call SnapshotService.
    """

    def __init__(
        self,
        repo: Repository,
        health: HealthService,
        controller: "AppController | None" = None,
    ) -> None:
        self.repo = repo
        self.health = health
        self.controller = controller

    def signal(
        self,
        plan_id: str,
        reason: TriggerReason,
        now_utc_ms: int,
    ) -> AutoDecision:
        if reason in {
            TriggerReason.MANUAL,
            TriggerReason.MANUAL_FORCE,
        }:
            return AutoDecision(
                plan_id=plan_id,
                action="no_action",
                reason=trigger_reason_label(reason),
                detail="A manual trigger is not recorded in the automatic pending state.",
            )

        mask = trigger_mask_for_reason(reason)
        if mask == 0:
            raise AppError(
                "TRIGGER_REASON_INVALID",
                f"Unsupported automatic reason: {reason.value}",
            )

        plan = self.repo.plan_details(plan_id)

        if plan.state == PlanState.ARCHIVED.value:
            return AutoDecision(
                plan_id=plan_id,
                action="skipped_archived",
                reason=reason.value,
                detail="An archived plan does not receive automatic triggers.",
            )

        if plan.state == PlanState.PAUSED.value:
            return AutoDecision(
                plan_id=plan_id,
                action="skipped_paused",
                reason=reason.value,
                detail="A paused plan does not receive automatic triggers.",
            )

        policy = self.repo.get_trigger_policy(plan_id)

        if (
            reason == TriggerReason.EVENT
            and not policy.on_change_enabled
        ):
            return AutoDecision(
                plan_id=plan_id,
                action="skipped_disabled",
                reason=reason.value,
                detail="Backup on change is disabled in the plan policy.",
            )

        if (
            reason in {
                TriggerReason.SCHEDULE,
                TriggerReason.STARTUP_CATCHUP,
            }
            and not policy.on_schedule_enabled
        ):
            return AutoDecision(
                plan_id=plan_id,
                action="skipped_disabled",
                reason=reason.value,
                detail="Scheduled backup is disabled in the plan policy.",
            )

        merged_mask = self.repo.mark_pending_trigger(
            plan_id,
            mask,
            now_utc_ms,
        )

        return AutoDecision(
            plan_id=plan_id,
            action="pending",
            reason=reason.value,
            detail="The automatic trigger was saved in the pending state.",
            trigger_mask=merged_mask,
        )

    def evaluate(
        self,
        plan_id: str,
        now_utc_ms: int,
        now_local: datetime,
        active_operation_plan_id: str | None = None,
    ) -> AutoDecision:
        plan = self.repo.plan_details(plan_id)

        if plan.pending_trigger_mask == 0:
            return AutoDecision(
                plan_id=plan_id,
                action="no_action",
                reason="none",
                detail="The plan has no pending automatic trigger.",
            )

        if plan.state == PlanState.ARCHIVED.value:
            return AutoDecision(
                plan_id=plan_id,
                action="skipped_archived",
                reason="pending",
                detail="An archived plan does not run automatic backups.",
                trigger_mask=plan.pending_trigger_mask,
            )

        if plan.state == PlanState.PAUSED.value:
            return AutoDecision(
                plan_id=plan_id,
                action="skipped_paused",
                reason="pending",
                detail="A paused plan does not run automatic backups.",
                trigger_mask=plan.pending_trigger_mask,
            )

        if self.repo.needs_recovery_for_plan(plan_id):
            return AutoDecision(
                plan_id=plan_id,
                action="blocked_recovery",
                reason="pending",
                detail="The plan needs recovery before an automatic backup.",
                trigger_mask=plan.pending_trigger_mask,
            )

        if any(i.code == "RESTORE_RECOVERY_REVIEW_REQUIRED"
               for i in self.repo.list_active_issues(plan_id)):
            return AutoDecision(plan_id, "blocked_recovery", "pending",
                                "An interrupted restore needs a check.", plan.pending_trigger_mask)

        if active_operation_plan_id == plan_id:
            return AutoDecision(
                plan_id=plan_id,
                action="already_running",
                reason="pending",
                detail="A heavy operation is already running for this plan.",
                trigger_mask=plan.pending_trigger_mask,
            )

        if active_operation_plan_id is not None:
            return AutoDecision(
                plan_id=plan_id,
                action="deferred_global_operation",
                reason="pending",
                detail="Another heavy operation is running in the application.",
                trigger_mask=plan.pending_trigger_mask,
            )

        policy = self.repo.get_trigger_policy(plan_id)

        lock_failure_at = self.repo.last_lock_failure_at(plan_id)
        if (plan.source_kind == SourceKind.FOLDER.value and lock_failure_at is not None
                and now_utc_ms < lock_failure_at + SOURCE_LOCK_COOLDOWN_MS):
            return AutoDecision(plan_id, "deferred_source_retry", "pending",
                                "Pause after a nested file was locked.", plan.pending_trigger_mask)
        source_probe = self.health.probe_source(plan)
        locked_issue = any(
            issue.code == "SOURCE_LOCKED"
            for issue in self.repo.list_active_issues(plan_id)
        )
        can_retry_in_worker = (
            source_probe.error_code == "SOURCE_LOCKED"
            and not (locked_issue and self.repo.last_backup_failed_from_lock(plan_id))
        )
        if (
            (not source_probe.available or not source_probe.readable)
            and not can_retry_in_worker
        ):
            error = AppError(
                source_probe.error_code or "SOURCE_PROBE_ERROR",
                source_probe.detail,
            )
            self.health.record_error(error, plan_id)
            return AutoDecision(
                plan_id=plan_id,
                action="blocked_source",
                reason="pending",
                detail=source_probe.detail,
                trigger_mask=plan.pending_trigger_mask,
            )

        if source_probe.readable:
            self.repo.resolve_plan_issue(plan_id, "SOURCE_LOCKED")
        storage_probe = self.health.probe_storage(plan)
        if not storage_probe.available or not storage_probe.writable:
            error = AppError(
                storage_probe.error_code or "STORAGE_PROBE_ERROR",
                storage_probe.detail,
            )
            self.health.record_error(error, plan_id)
            return AutoDecision(
                plan_id=plan_id,
                action="blocked_storage",
                reason="pending",
                detail=storage_probe.detail,
                trigger_mask=plan.pending_trigger_mask,
            )

        pending_since = plan.pending_trigger_since_utc_ms
        if pending_since is None:
            return AutoDecision(
                plan_id=plan_id,
                action="blocked_invalid_state",
                reason="pending",
                detail="The pending trigger has no timestamp.",
                trigger_mask=plan.pending_trigger_mask,
            )

        debounce_ms = policy.change_debounce_seconds * 1000
        event_pending = bool(
            plan.pending_trigger_mask & TRIGGER_MASK_EVENT
        )

        if (
            event_pending
            and now_utc_ms - pending_since < debounce_ms
        ):
            return AutoDecision(
                plan_id=plan_id,
                action="deferred_debounce",
                reason="event",
                detail="Waiting for the debounce to finish after the source changed.",
                trigger_mask=plan.pending_trigger_mask,
            )

        if (
            policy.quiet_hours_enabled
            and is_in_quiet_hours(
                now_local,
                policy.quiet_hours_start_local,
                policy.quiet_hours_end_local,
            )
        ):
            return AutoDecision(
                plan_id=plan_id,
                action="deferred_quiet_hours",
                reason="pending",
                detail="The automatic backup is postponed because of quiet hours.",
                trigger_mask=plan.pending_trigger_mask,
            )

        if plan.last_auto_backup_at_utc_ms is not None:
            min_interval_ms = (
                policy.min_auto_interval_minutes * 60 * 1000
            )
            if now_utc_ms - plan.last_auto_backup_at_utc_ms < min_interval_ms:
                return AutoDecision(
                    plan_id=plan_id,
                    action="deferred_min_interval",
                    reason="pending",
                    detail="The minimum interval between automatic backups has not passed.",
                    trigger_mask=plan.pending_trigger_mask,
                )

        return AutoDecision(
            plan_id=plan_id,
            action="queued",
            reason="pending",
            detail="The plan is ready to queue an automatic backup.",
            trigger_mask=plan.pending_trigger_mask,
        )

    def evaluate_pending(
        self,
        now_utc_ms: int,
        now_local: datetime,
        active_operation_plan_id: str | None = None,
    ) -> list[AutoDecision]:
        return [
            self.evaluate(
                plan.id,
                now_utc_ms,
                now_local,
                active_operation_plan_id,
            )
            for plan in self.repo.pending_plans()
        ]

    def complete_automatic_run(
        self,
        plan_id: str,
        completed_at_utc_ms: int,
        success_or_no_changes: bool,
    ) -> None:
        if success_or_no_changes:
            self.repo.update_last_auto_backup(
                plan_id,
                completed_at_utc_ms,
            )
            self.repo.clear_pending_trigger(plan_id)


class PollingChangeWatcher:
    """Deterministic schedule/startup trigger source.

    This class:
    - does not start a worker;
    - does not call SnapshotService;
    - does not create Operations;
    - only creates pending SCHEDULE/STARTUP_CATCHUP triggers
      through AutoBackupCoordinator.
    """

    def __init__(
        self,
        repo: Repository,
        health: HealthService,
        coordinator: AutoBackupCoordinator,
    ) -> None:
        self.repo = repo
        self.health = health
        self.coordinator = coordinator

    def poll_once(
        self,
        now_utc_ms: int,
    ) -> list[AutoDecision]:
        decisions: list[AutoDecision] = []

        for summary in self.repo.plan_summaries(False):
            if summary.state != PlanState.ENABLED.value:
                continue

            if not summary.on_change_enabled:
                continue

            plan = self.repo.plan_details(summary.id)

            try:
                if (
                    not Path(plan.source_path).exists()
                    and plan.source_identity_json
                ):
                    self.repo.find_relocated_source(plan)
                    decision = self.coordinator.signal(
                        plan.id,
                        TriggerReason.EVENT,
                        now_utc_ms,
                    )
                    decisions.append(decision)
                    continue

                self.repo.assert_source_identity(plan)
                descriptor = PathPolicy.validate_source(
                    plan.source_path
                )
                fingerprint, current_total_bytes = source_metadata_scan(descriptor)

            except AppError as exc:
                self.health.record_error(exc, plan.id)
                if self.coordinator.controller is not None:
                    ui = self.coordinator.controller
                    ui.notify_event(
                        exc.code,
                        ui.i18n.tr("notification.source_unavailable_title"),
                        ui.i18n.error_message(exc),
                        NotificationSeverity.ERROR,
                        plan.id,
                    )
                decisions.append(
                    AutoDecision(
                        plan_id=plan.id,
                        action="blocked_source",
                        reason=TriggerReason.EVENT.value,
                        detail=exc.user_message,
                        trigger_mask=plan.pending_trigger_mask,
                    )
                )
                continue

            previous_fingerprint = plan.last_source_scan_fingerprint

            if not previous_fingerprint:
                self.repo.update_last_source_scan_fingerprint(
                    plan.id,
                    fingerprint,
                )
                decisions.append(
                    AutoDecision(
                        plan_id=plan.id,
                        action="baseline_recorded",
                        reason=TriggerReason.EVENT.value,
                        detail="The initial source metadata fingerprint was saved.",
                    )
                )
                continue

            if previous_fingerprint == fingerprint:
                decisions.append(
                    AutoDecision(
                        plan_id=plan.id,
                        action="no_change",
                        reason=TriggerReason.EVENT.value,
                        detail="No source metadata changes were found.",
                        trigger_mask=plan.pending_trigger_mask,
                    )
                )
                continue

            self.repo.update_last_source_scan_fingerprint(
                plan.id,
                fingerprint,
            )

            threshold_bytes = self.repo.get_trigger_policy(plan.id).change_threshold_bytes
            if threshold_bytes > 0 and not (plan.pending_trigger_mask & TRIGGER_MASK_EVENT):
                last_bytes = self.repo.latest_committed_logical_size(plan.id)
                if not change_threshold_reached(threshold_bytes, current_total_bytes, last_bytes):
                    decisions.append(
                        AutoDecision(
                            plan_id=plan.id,
                            action="deferred_threshold",
                            reason=TriggerReason.EVENT.value,
                            detail="The size change is below the threshold.",
                            trigger_mask=plan.pending_trigger_mask,
                        )
                    )
                    continue

            decision = self.coordinator.signal(
                plan.id,
                TriggerReason.EVENT,
                now_utc_ms,
            )

            if decision.action == "pending":
                self.repo.activity(
                    "info",
                    "AUTO_SOURCE_CHANGE_DETECTED",
                    "A source change was detected. An automatic backup is waiting to run.",
                    plan.id,
                    details={
                        "previous_fingerprint": previous_fingerprint,
                        "current_fingerprint": fingerprint,
                        "trigger_mask": decision.trigger_mask,
                    },
                )

            decisions.append(decision)

        return decisions


class ScheduleCoordinator:
    """Removes the contents of a dynamic layout, including nested layouts."""

    def __init__(
        self,
        repo: Repository,
        coordinator: AutoBackupCoordinator,
    ) -> None:
        self.repo = repo
        self.coordinator = coordinator

    @staticmethod
    def _schedule_due(
        plan: PlanDetails,
        now_utc_ms: int,
    ) -> bool:
        if plan.last_auto_backup_at_utc_ms is None:
            return True

        interval_ms = (
            plan.schedule_interval_minutes
            * 60
            * 1000
        )
        return (
            now_utc_ms - plan.last_auto_backup_at_utc_ms
            >= interval_ms
        )

    def tick(
        self,
        now_utc_ms: int,
    ) -> list[AutoDecision]:
        decisions: list[AutoDecision] = []

        for summary in self.repo.plan_summaries(False):
            if summary.state != PlanState.ENABLED.value:
                continue

            if not summary.on_schedule_enabled:
                continue

            plan = self.repo.plan_details(summary.id)

            if has_trigger_mask(
                plan.pending_trigger_mask,
                TRIGGER_MASK_SCHEDULE,
            ):
                decisions.append(
                    AutoDecision(
                        plan_id=plan.id,
                        action="already_pending",
                        reason=TriggerReason.SCHEDULE.value,
                        detail="The schedule trigger is already waiting to run.",
                        trigger_mask=plan.pending_trigger_mask,
                    )
                )
                continue

            if not self._schedule_due(plan, now_utc_ms):
                decisions.append(
                    AutoDecision(
                        plan_id=plan.id,
                        action="not_due",
                        reason=TriggerReason.SCHEDULE.value,
                        detail="The scheduled backup interval has not come yet.",
                        trigger_mask=plan.pending_trigger_mask,
                    )
                )
                continue

            decision = self.coordinator.signal(
                plan.id,
                TriggerReason.SCHEDULE,
                now_utc_ms,
            )

            if decision.action == "pending":
                self.repo.activity(
                    "info",
                    "AUTO_TRIGGER_SCHEDULE",
                    "The plan is waiting for a scheduled automatic backup.",
                    plan.id,
                    details={
                        "schedule_interval_minutes": (
                            plan.schedule_interval_minutes
                        ),
                        "trigger_mask": decision.trigger_mask,
                    },
                )

            decisions.append(decision)

        return decisions

    def startup_catchup(
        self,
        now_utc_ms: int,
    ) -> list[AutoDecision]:
        decisions: list[AutoDecision] = []

        grace_ms = (
            STARTUP_CATCHUP_GRACE_MINUTES
            * 60
            * 1000
        )

        for summary in self.repo.plan_summaries(False):
            if summary.state != PlanState.ENABLED.value:
                continue

            if not summary.on_schedule_enabled:
                continue

            plan = self.repo.plan_details(summary.id)

            if plan.pending_trigger_mask != 0:
                decisions.append(
                    AutoDecision(
                        plan_id=plan.id,
                        action="already_pending",
                        reason=TriggerReason.STARTUP_CATCHUP.value,
                        detail="The plan already has a pending automatic trigger.",
                        trigger_mask=plan.pending_trigger_mask,
                    )
                )
                continue

            if plan.last_auto_backup_at_utc_ms is not None:
                elapsed_ms = (
                    now_utc_ms
                    - plan.last_auto_backup_at_utc_ms
                )

                if elapsed_ms < grace_ms:
                    decisions.append(
                        AutoDecision(
                            plan_id=plan.id,
                            action="not_due",
                            reason=TriggerReason.STARTUP_CATCHUP.value,
                            detail=(
                                "The last automatic backup ran "
                                "recently; a startup catch-up is not needed."
                            ),
                        )
                    )
                    continue

            if not self._schedule_due(plan, now_utc_ms):
                decisions.append(
                    AutoDecision(
                        plan_id=plan.id,
                        action="not_due",
                        reason=TriggerReason.STARTUP_CATCHUP.value,
                        detail="The schedule interval after a restart has not come yet.",
                    )
                )
                continue

            decision = self.coordinator.signal(
                plan.id,
                TriggerReason.STARTUP_CATCHUP,
                now_utc_ms,
            )

            if decision.action == "pending":
                self.repo.activity(
                    "info",
                    "AUTO_TRIGGER_STARTUP_CATCHUP",
                    "The plan is waiting for an automatic backup after the application started.",
                    plan.id,
                    details={
                        "schedule_interval_minutes": (
                            plan.schedule_interval_minutes
                        ),
                        "trigger_mask": decision.trigger_mask,
                    },
                )

            decisions.append(decision)

        return decisions


class OperationWorker(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(self, controller: "AppController") -> None:
        super().__init__()
        self.controller = controller
        self.cancel_event = threading.Event()
        self._command_lock = threading.Lock()
        self._command_running = False
        self._current_operation_id: str | None = None
        self._last_progress_monotonic = 0.0
        self._last_progress_event: ProgressEvent | None = None

    @property
    def command_running(self) -> bool:
        with self._command_lock:
            return self._command_running

    def cancel(self) -> None:
        with self._command_lock:
            self.cancel_event.set()

    def emit_progress(self, event: ProgressEvent) -> None:
        now = time.monotonic()
        previous = self._last_progress_event

        phase_changed = (
            previous is None
            or event.title != previous.title
            or event.cancellable != previous.cancellable
        )
        completed = (
            event.total_files > 0
            and event.completed_files >= event.total_files
        ) or (
            event.total_bytes > 0
            and event.completed_bytes >= event.total_bytes
        )

        self._last_progress_event = event

        if (
            not phase_changed
            and not completed
            and now - self._last_progress_monotonic < 0.10
        ):
            return

        self._last_progress_monotonic = now
        self.progress.emit(event)

    @Slot(object)
    def execute(self, command: dict[str, Any]) -> None:
        operation_id = command["operation_id"]
        plan_id = command.get("plan_id")
        trigger_reason = command.get("trigger_reason")

        token = command.get("_cancel_event")

        if (
            token is None
            and self.controller.active_operation_id == operation_id
        ):
            token = getattr(
                self.controller,
                "_active_cancel_event",
                None,
            )

        if token is None:
            token = self.cancel_event

        if not isinstance(token, threading.Event):
            raise TypeError(
                "Command cancellation token must be threading.Event"
            )

        with self._command_lock:
            if self._command_running:
                logging.error(
                    "Worker rejected overlapping command %s",
                    operation_id,
                )
                return

            self._command_running = True
            self._current_operation_id = operation_id
            self.cancel_event = token
            self._last_progress_monotonic = 0.0
            self._last_progress_event = None

        try:
            self.controller.repo.operation_state(
                operation_id,
                "running",
            )

            if token.is_set():
                raise CancelledError()

            kind = command["kind"]
            progress = self.emit_progress
            cancelled = token.is_set

            if kind == OperationType.CHECK_DATABASE.value:
                self.controller.db.verify_contract()

                if cancelled():
                    raise CancelledError()

                result = OperationResult(
                    operation_id,
                    True,
                    "The database check finished.",
                    result_code="DATABASE_CHECK_SUCCEEDED",
                )

            elif kind == OperationType.LIST_SNAPSHOT_FILES.value:
                result = self.controller.verifier.list_snapshot_files(
                    operation_id,
                    command["snapshot_id"],
                    progress,
                    cancelled,
                )

            elif kind == OperationType.REFRESH_HEALTH.value:
                result = self.controller._perform_health_refresh(
                    operation_id,
                    cancelled,
                )

            elif kind == OperationType.RUNTIME_TICK.value:
                result = self.controller._perform_runtime_tick(
                    operation_id,
                    command["now_utc_ms"],
                    command["now_local_iso"],
                    cancelled,
                )

            elif kind == OperationType.BACKUP.value:
                result = self.controller.snapshot_service.create(
                    operation_id,
                    command["plan_id"],
                    bool(command.get("force")),
                    progress,
                    cancelled,
                )

            elif kind == OperationType.TEST_RESTORE.value:
                result = self.controller.restore_test_service.run(
                    operation_id, command["snapshot_id"], progress, cancelled,
                    str(command.get("mode", "full")),
                )

            elif kind == OperationType.VERIFY.value:
                result = self.controller.verifier.verify(
                    operation_id,
                    command["snapshot_id"],
                    progress,
                    cancelled,
                )

            elif kind == OperationType.RESTORE_BESIDE.value:
                result = self.controller.restore_service.restore(
                    operation_id,
                    command["snapshot_id"],
                    progress,
                    cancelled,
                )

            elif kind == OperationType.RESTORE_TO_DIRECTORY.value:
                result = (
                    self.controller.restore_service
                    .restore_to_directory(
                        operation_id,
                        command["snapshot_id"],
                        command["target_directory"],
                        progress,
                        cancelled,
                    )
                )

            elif kind == OperationType.RESTORE_REPLACE_FILE.value:
                result = (
                    self.controller.restore_service
                    .replace_original_file(
                        operation_id,
                        command["snapshot_id"],
                        progress,
                        cancelled,
                    )
                )

            elif kind == OperationType.RESTORE_SELECTED_FILES.value:
                result = (
                    self.controller.restore_service
                    .restore_selected_files(
                        operation_id,
                        command["snapshot_id"],
                        command["selected_relative_paths"],
                        command["target_directory"],
                        progress,
                        cancelled,
                    )
                )

            elif kind == OperationType.DELETE_PLAN_DATA.value:
                result = (
                    self.controller.delete_service.delete_plan_data(
                        operation_id,
                        command["plan_id"],
                        command["preview_token"],
                        bool(command.get("approve_pinned")),
                        progress,
                        cancelled,
                    )
                )

            elif kind == OperationType.APPLY_RETENTION.value:
                try:
                    preview = self.controller.retention_service.apply(
                        command["plan_id"], command.get("preview_token"), progress,
                        cancelled, operation_id,
                    )
                    self.controller.repo.operation_state(operation_id, "succeeded")
                    result = OperationResult(
                        operation_id, True,
                        f"The retention policy was applied; versions deleted: {preview.delete_count}.",
                        result_code="RETENTION_APPLIED",
                    )
                except CancelledError:
                    raise

            elif kind == OperationType.DELETE_SNAPSHOTS.value:
                result = self.controller.delete_service.delete(
                    operation_id,
                    command["snapshot_ids"],
                    progress,
                    cancelled,
                )

            elif kind == OperationType.RECOVER_PLAN.value:
                result = self.controller.recovery_service.review_plan(
                    operation_id,
                    command["plan_id"],
                    progress,
                    cancelled,
                )

            else:
                raise AppError(
                    "OPERATION_UNSUPPORTED",
                    "The operation is not supported.",
                )

        except AppError as exc:
            state = (
                "cancelled"
                if exc.code == "OPERATION_CANCELLED"
                else "failed"
            )
            self.controller.repo.operation_state(
                operation_id,
                state,
                exc,
            )
            result = OperationResult(
                operation_id,
                False,
                exc.user_message,
                exc.code,
            )

        except Exception:
            logging.exception("Unexpected worker failure")
            error = AppError(
                "WORKER_EXECUTION_FAILED",
                "Unexpected operation error.",
                traceback.format_exc(),
            )
            self.controller.repo.operation_state(
                operation_id,
                "failed",
                error,
            )
            result = OperationResult(
                operation_id,
                False,
                error.user_message,
                error.code,
            )

        finally:
            with self._command_lock:
                self._command_running = False
                self._current_operation_id = None
                self.cancel_event = threading.Event()

        # Terminal states of the services are not changed,
        # in particular needs_recovery.
        row = self.controller.repo.operation(operation_id)

        if row["state"] in {"queued", "running"}:
            error = (
                None
                if result.success
                else AppError(
                    result.error_code or "WORKER_EXECUTION_FAILED",
                    result.message,
                )
            )
            state = (
                "succeeded"
                if result.success
                else (
                    "cancelled"
                    if result.error_code == "OPERATION_CANCELLED"
                    else "failed"
                )
            )
            self.controller.repo.operation_state(
                operation_id,
                state,
                error,
            )

        result.trigger_reason = trigger_reason
        result.plan_id = plan_id
        self.finished.emit(result)

def clear_layout_recursively(layout: QLayout) -> None:
    """Windows Run registry adapter.

    It is not part of the backup core. It does not start the application,
    does not change the backup policy and does not access SQLite directly.
    """
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue

        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
            continue

        child_layout = item.layout()
        if child_layout is not None:
            clear_layout_recursively(child_layout)
            child_layout.deleteLater()


class StatusBadge(QFrame):
    COLORS = {
        HealthState.PROTECTED.value: "#4fca8a",
        HealthState.ATTENTION.value: "#f3bd4f",
        HealthState.ERROR.value: "#ef6974",
        HealthState.PAUSED.value: "#9aa8bc",
        HealthState.UNPROTECTED.value: "#9aa8bc",
        HealthState.RUNNING.value: "#7c9cff",
        HealthState.ARCHIVED.value: "#6b7c96",
    }

    def __init__(self, state: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusBadge")
        self.state = state
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(5)
        self.text_label = QLabel(text)
        layout.addWidget(self.text_label)
        self.set_state(state, text)

    def set_state(self, state: str, text: str) -> None:
        self.state = state
        color = self.COLORS.get(state, "#9aa8bc")
        self.text_label.setStyleSheet(f"color: {color}; font-weight: 600; background: transparent;")
        self.text_label.setText(text)
        self.setStyleSheet(
            f"QFrame#statusBadge {{ background: {color}22; border: 1px solid {color}66; border-radius: 12px; }}"
        )


class MetricCard(QFrame):
    def __init__(self, caption: str, value: str, detail: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        caption_label = QLabel(caption)
        caption_label.setProperty("role", "muted")
        self.value_label = QLabel(value)
        self.value_label.setProperty("role", "metric")
        self.detail_label = QLabel(detail)
        self.detail_label.setProperty("role", "muted")
        layout.addWidget(caption_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.detail_label)


class PlanCard(QFrame):
    clicked = Signal(str)

    def __init__(self, plan: PlanSummary, health: PlanHealth, selected: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plan_id = plan.id
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(84)
        self.setProperty("role", "card")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        color = StatusBadge.COLORS.get(health.state, "#9aa8bc")
        stripe = QFrame()
        stripe.setFixedWidth(4)
        stripe.setStyleSheet(f"background: {color}; border-radius: 2px;")
        layout.addWidget(stripe)
        info = QVBoxLayout()
        info.setSpacing(2)
        title = QLabel(plan.display_name)
        title.setProperty("role", "subtitle")
        path = QLabel()
        path.setProperty("role", "muted")
        path.setStyleSheet("font-size: 11px;")
        path.setToolTip(plan.source_path)
        path.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )

        metrics = path.fontMetrics()
        path.setText(
            metrics.elidedText(
                plan.source_path,
                Qt.TextElideMode.ElideMiddle,
                250,
            )
        )
        state = QLabel(health.title)
        state.setStyleSheet(f"color: {color}; font-size: 11px;")
        info.addWidget(title)
        info.addWidget(path)
        info.addWidget(state)
        layout.addLayout(info, 1)
        border = color if selected else "#303b50"
        width = "2px" if selected else "1px"
        self.setStyleSheet(
            f"QFrame[role='card'] {{ background:#181e28; border:{width} solid {border}; border-radius:8px; }}"
            "QFrame[role='card']:hover { background:#202837; }"
        )

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.plan_id)
        super().mousePressEvent(event)


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line_height > 0:
                x = area.x()
                y += line_height + self.spacing()
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class EmptyState(QWidget):
    def __init__(
        self,
        title: str,
        detail: str,
        action_text: str | None = None,
        action: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(10)
        title_label = QLabel(title)
        title_label.setProperty("role", "title")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail_label = QLabel(detail)
        detail_label.setProperty("role", "muted")
        detail_label.setWordWrap(True)
        detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        layout.addWidget(detail_label)
        if action_text and action:
            button = QPushButton(action_text)
            button.setProperty("primary", True)
            button.clicked.connect(action)
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)


class DropOverlay(QFrame):
    def __init__(
        self,
        parent: QWidget,
        i18n: I18nService,
        title: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dropOverlay")
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.setStyleSheet(
            """
            QFrame#dropOverlay {
                background-color: rgba(109, 124, 255, 38);
                border: 2px dashed #7c9cff;
                border-radius: 10px;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title_label = QLabel(title or i18n.tr("drop.title"))
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet(
            "font-size:18px;font-weight:600;color:#edf2f8;"
        )

        detail_label = QLabel(detail or i18n.tr("drop.detail"))
        detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail_label.setWordWrap(True)
        detail_label.setStyleSheet("color:#c9d9f4;")
        self.title_label = title_label
        self.detail_label = detail_label

        layout.addWidget(title_label)
        layout.addWidget(detail_label)
        self.hide()


class PathDropTarget(QObject):
    """One drag-and-drop path handler. It only fills a field or calls a handler."""

    _DRAG_EVENTS = frozenset({
        QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.DragLeave, QEvent.Type.Drop,
    })
    _HANDLED_EVENTS = _DRAG_EVENTS | {QEvent.Type.Resize}

    def __init__(
        self,
        widget: QWidget,
        i18n: I18nService,
        kind: str | Callable[[], str | None],
        on_path: Callable[[Path], None],
        allow_multiple: bool = False,
        on_many: Callable[[list[Path]], None] | None = None,
        title: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(widget)
        self.widget = widget
        self.i18n = i18n
        self.kind = kind
        self.on_path = on_path
        self.allow_multiple = allow_multiple
        self.on_many = on_many
        self.title_override = title
        self.overlay = DropOverlay(widget, i18n, title, detail)
        widget._drop_overlay = self.overlay
        widget.setAcceptDrops(True)
        widget.installEventFilter(self)

    def _kind(self) -> str | None:
        kind = self.kind() if callable(self.kind) else self.kind
        return kind or None

    def _message(self, exc: AppError) -> str:
        key = f"error.{exc.code}"
        if self.i18n.has_key(key):
            return self.i18n.tr(key)
        return exc.user_message

    def _local_paths(self, mime: Any) -> list[Path]:
        if mime is None or not mime.hasUrls():
            return []
        urls = list(mime.urls())
        if not urls:
            return []
        if len(urls) != 1 and not self.allow_multiple:
            raise AppError("DROP_MULTIPLE_ITEMS", self.i18n.tr("error.DROP_MULTIPLE_ITEMS"))
        paths: list[Path] = []
        for url in urls:
            if not url.isLocalFile():
                raise AppError("DROP_UNSUPPORTED_SOURCE", self.i18n.tr("error.DROP_UNSUPPORTED_SOURCE"))
            path = Path(url.toLocalFile())
            if not path.is_absolute():
                raise AppError("DROP_UNSUPPORTED_SOURCE", self.i18n.tr("error.DROP_UNSUPPORTED_SOURCE"))
            paths.append(path)
        return paths

    def _checked(self, path: Path, kind: str) -> Path:
        if path.suffix.casefold() == ".lnk":
            raise AppError("DROP_SHORTCUT", self.i18n.tr("error.DROP_SHORTCUT"))
        descriptor = PathPolicy.validate_source(str(path))
        if kind == "folder" and descriptor.kind != SourceKind.FOLDER:
            raise AppError("DROP_FOLDER_ONLY", self.i18n.tr("error.DROP_FOLDER_ONLY"))
        if kind == "file" and descriptor.kind != SourceKind.FILE:
            raise AppError("DROP_FILE_ONLY", self.i18n.tr("error.DROP_FILE_ONLY"))
        return descriptor.path

    def _warn(self, exc: AppError) -> None:
        self.overlay.hide()
        QMessageBox.warning(self.widget, self.i18n.tr("editor.drop_error_title"), self._message(exc))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # The filter is also called while the widget is being destroyed (ChildRemoved etc.); before touching
        # its own state we drop all events it does not handle.
        if event.type() not in self._HANDLED_EVENTS or watched is not self.widget:
            return False
        if event.type() == QEvent.Type.Resize:
            self.overlay.setGeometry(self.widget.rect())
            return False
        if event.type() not in self._DRAG_EVENTS:
            return False
        kind = self._kind()
        if kind is None:
            event.ignore()
            self.overlay.hide()
            return True
        if event.type() == QEvent.Type.DragLeave:
            self.overlay.hide()
            event.accept()
            return True
        try:
            paths = self._local_paths(event.mimeData())
        except AppError as exc:
            self.overlay.hide()
            if event.type() == QEvent.Type.Drop:
                self._warn(exc)
            event.ignore()
            return True
        if not paths:
            event.ignore()
            return True
        if event.type() in {QEvent.Type.DragEnter, QEvent.Type.DragMove}:
            if kind == "folder" and not self.title_override:
                self.overlay.title_label.setText(self.i18n.tr("drop.folder_title"))
                self.overlay.detail_label.setText(self.i18n.tr("drop.folder_detail"))
            self.overlay.setGeometry(self.widget.rect())
            self.overlay.show()
            self.overlay.raise_()
            event.acceptProposedAction()
            return True
        self.overlay.hide()
        try:
            checked = [self._checked(path, kind) for path in paths]
        except AppError as exc:
            self._warn(exc)
            event.ignore()
            return True
        if len(checked) > 1 and self.on_many is not None:
            self.on_many(checked)
        else:
            self.on_path(checked[0])
        event.acceptProposedAction()
        return True


def build_indicator_qss() -> str:
    """Stronger checkbox and radio indicators; draws the tick icon at startup."""
    tick = ""
    try:
        from PySide6.QtGui import QImage, QPainter, QPen
        image = QImage(14, 14, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline([QPoint(3, 7), QPoint(6, 10), QPoint(11, 4)])
        painter.end()
        icon_path = Path(tempfile.gettempdir()) / "amvibackup_check_v1.png"
        if image.save(str(icon_path)):
            tick = f"image: url({icon_path.as_posix()});"
    except Exception:
        logging.exception("Checkbox tick icon unavailable")
    return f"""
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; border: 2px solid #8493ad; background-color: #0e121a; }}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: #b3c3e6; background-color: #182030; }}
QCheckBox::indicator:checked {{ background-color: #6d7cff; border-color: #6d7cff; {tick} }}
QCheckBox::indicator:checked:hover {{ background-color: #7c9cff; border-color: #7c9cff; }}
QRadioButton::indicator:checked {{ border-color: #6d7cff; background-color: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 #ffffff, stop:0.4 #ffffff, stop:0.45 #6d7cff, stop:1 #6d7cff); }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: #3a475e; background-color: #151b26; }}
QCheckBox::indicator:checked:disabled {{ background-color: #3d4670; border-color: #3d4670; }}
"""

class HelpMark(QLabel):
    """Non-clickable "?" marker: hovering shows an explanation tooltip immediately."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__("?", parent)
        self.setFixedSize(18, 18)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("?")
        self.setStyleSheet(
            "QLabel{background:#232c3d;border:1px solid #4a5873;border-radius:9px;"
            "color:#9aa8bc;font-size:11px;font-weight:700;}"
            "QLabel:hover{background:#2f4a8a;border-color:#7c9cff;color:#ffffff;}")
        self.set_help_text(text)

    def set_help_text(self, text: str) -> None:
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        self._html = f"<p style='white-space:normal;width:340px;'>{escaped}</p>"
        self.setToolTip(self._html)

    def enterEvent(self, event: Any) -> None:
        QToolTip.showText(self.mapToGlobal(QPoint(self.width(), self.height())), self._html, self)
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        QToolTip.hideText()
        super().leaveEvent(event)


class WheelGuard(QObject):
    """Stops mouse wheel from changing spin boxes and combo boxes; scrolls the form instead."""

    def __init__(self, scroll: QScrollArea | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._scroll = scroll

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel:
            if self._scroll is not None:
                bar = self._scroll.verticalScrollBar()
                delta = event.angleDelta().y()
                if delta:
                    bar.setValue(bar.value() - round(delta * bar.singleStep() * 3 / 120))
            event.accept()
            return True
        return False


def install_wheel_guard(root: QWidget, scroll: QScrollArea | None) -> WheelGuard:
    guard = WheelGuard(scroll, root)
    for widget in root.findChildren(QAbstractSpinBox) + root.findChildren(QComboBox):
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        widget.installEventFilter(guard)
        for line in widget.findChildren(QLineEdit):
            line.installEventFilter(guard)
    return guard


class PlanEditor(QDialog):
    """Plan dialog: one window, Basic (single page) and Advanced (full options) modes.

    Advanced widgets are the single source of truth. Basic controls are thin presets
    that write into them, and are re-derived from them when switching back.
    """

    _last_advanced = False
    SCAN_MAX_FILES = 300_000
    SCAN_MAX_SECONDS = 6.0
    BASIC_INTERVALS = (5, 10, 30, 60, 180)
    BASIC_SCHEDULES = (60, 360, 1440, 10080)
    KEEP_LIGHT = 5
    KEEP_MAX = 30

    def __init__(
        self,
        controller: "AppController",
        plan: PlanDetails | None = None,
        parent: QWidget | None = None,
        start_advanced: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.plan = plan
        self.created_plan_id: str | None = None
        self.start_first_backup = False
        self._scan_stop = threading.Event()
        self._scan_state: dict[str, Any] = {"token": 0, "done": True, "ok": True, "bytes": 0, "files": 0, "partial": False}
        self._scan_path = ""
        tr = controller.i18n.tr

        self.setWindowTitle(tr("editor.new_title") if plan is None else tr("editor.edit_title"))
        self.resize(960, 740)
        self.setMinimumSize(780, 560)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel(self.windowTitle())
        title.setProperty("role", "title")
        header.addWidget(title)
        header.addStretch()
        seg = QFrame()
        seg.setObjectName("segFrame")
        seg.setStyleSheet(
            "QFrame#segFrame{background:#151c27;border:1px solid #303b50;border-radius:8px;}"
            "QFrame#segFrame QPushButton{background:transparent;border:none;border-radius:6px;"
            "padding:7px 24px;color:#9aa8bc;font-weight:600;}"
            "QFrame#segFrame QPushButton:hover{color:#edf2f8;}"
            "QFrame#segFrame QPushButton:checked{background:#6d7cff;color:#ffffff;}")
        seg_layout = QHBoxLayout(seg)
        seg_layout.setContentsMargins(3, 3, 3, 3)
        seg_layout.setSpacing(2)
        self.basic_button = QPushButton(tr("planform.mode.basic"))
        self.advanced_button = QPushButton(tr("planform.mode.advanced"))
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for button in (self.basic_button, self.advanced_button):
            button.setCheckable(True)
            button.setMinimumWidth(130)
            self._mode_group.addButton(button)
            seg_layout.addWidget(button)
        header.addWidget(seg)
        root.addLayout(header)
        self.switch_hint = QLabel()
        self.switch_hint.setProperty("role", "muted")
        self.switch_hint.setWordWrap(True)
        root.addWidget(self.switch_hint)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_col = QWidget()
        col = QVBoxLayout(form_col)
        col.setContentsMargins(0, 0, 8, 0)
        col.setSpacing(10)
        scroll.setWidget(form_col)
        body.addWidget(scroll, 1)

        # shared: source
        self.name_edit = QLineEdit(plan.display_name if plan else "")
        self.source_edit = QLineEdit(plan.source_path if plan else "")
        self.storage_edit = QLineEdit(plan.storage_path if plan else "")
        card, lay = self._card(tr("planform.card.source"))
        self.drop_zone = QLabel(tr("planform.drop"))
        self.drop_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_zone.setMinimumHeight(54)
        self.drop_zone.setStyleSheet(
            "QLabel{border:2px dashed #3a475e;border-radius:8px;color:#9aa8bc;background:#151c27;}")
        lay.addWidget(self.drop_zone)
        row = QHBoxLayout()
        row.addWidget(self.source_edit, 1)
        folder_button = QPushButton(tr("planform.pick.folder"))
        folder_button.clicked.connect(lambda _=False: self.pick_source_folder())
        file_button = QPushButton(tr("planform.pick.file"))
        file_button.clicked.connect(lambda _=False: self.pick_source_file())
        row.addWidget(folder_button)
        row.addWidget(file_button)
        lay.addLayout(row)
        self.source_hint = QLabel()
        self.source_hint.setWordWrap(True)
        lay.addWidget(self.source_hint)
        col.addWidget(card)

        # shared: storage + name
        card, lay = self._card(tr("planform.card.storage"))
        row = QHBoxLayout()
        row.addWidget(self.storage_edit, 1)
        storage_button = QPushButton(tr("editor.browse"))
        storage_button.clicked.connect(lambda _=False: self.pick_storage())
        row.addWidget(storage_button)
        lay.addLayout(row)
        self.storage_history_combo = QComboBox()
        self.storage_history_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.storage_history_combo.setMinimumContentsLength(12)
        self.storage_history_combo.activated.connect(self._on_storage_history_picked)
        history_row = QHBoxLayout()
        history_row.addWidget(self.storage_history_combo, 1)
        history_row.addWidget(self._help_mark("folders.history_help.storage_history"))
        lay.addLayout(history_row)
        self._load_storage_history()
        self.storage_hint = QLabel()
        self.storage_hint.setWordWrap(True)
        lay.addWidget(self.storage_hint)
        self.storage_basic_note = QLabel(tr("planform.storage_note"))
        self.storage_basic_note.setProperty("role", "muted")
        self.storage_basic_note.setWordWrap(True)
        lay.addWidget(self.storage_basic_note)
        name_row = QHBoxLayout()
        name_row.addWidget(self._labeled(tr("planform.name"), "planform.help.name"))
        name_row.addWidget(self.name_edit, 1)
        lay.addLayout(name_row)
        col.addWidget(card)

        # basic page
        self.basic_widget = QWidget()
        basic_layout = QVBoxLayout(self.basic_widget)
        basic_layout.setContentsMargins(0, 0, 0, 0)
        basic_layout.setSpacing(10)
        card, lay = self._card(tr("planform.card.freq"))
        self._freq_group = QButtonGroup(self)
        self._freq_group.setExclusive(True)
        self.freq_auto_radio = QRadioButton(tr("planform.freq.auto"))
        self.freq_schedule_radio = QRadioButton(tr("planform.freq.schedule"))
        self.freq_manual_radio = QRadioButton(tr("planform.freq.manual"))
        self.freq_custom_radio = QRadioButton(tr("planform.freq.custom"))
        for radio in (self.freq_auto_radio, self.freq_schedule_radio, self.freq_manual_radio, self.freq_custom_radio):
            self._freq_group.addButton(radio)
            radio.clicked.connect(lambda _=False: self._apply_freq_preset())
        self.basic_interval_combo = QComboBox()
        for value in self.BASIC_INTERVALS:
            self.basic_interval_combo.addItem(self._minutes_text(value), value)
        self.basic_schedule_combo = QComboBox()
        for value in self.BASIC_SCHEDULES:
            self.basic_schedule_combo.addItem(self._minutes_text(value), value)
        self.basic_interval_combo.activated.connect(lambda _=0: self._apply_freq_preset())
        self.basic_schedule_combo.activated.connect(lambda _=0: self._apply_freq_preset())
        self.basic_threshold_combo = self._new_threshold_combo()
        self.basic_threshold_combo.activated.connect(lambda _=0: self._apply_threshold_from_basic())
        lay.addWidget(self._with_help(self.freq_auto_radio, "planform.help.auto"))
        lay.addWidget(self._indented(tr("planform.freq.auto.d")))
        interval_row = QHBoxLayout()
        interval_row.setContentsMargins(24, 0, 0, 0)
        interval_row.addWidget(QLabel(tr("planform.freq.interval")))
        interval_row.addWidget(self.basic_interval_combo)
        interval_row.addWidget(self._help_mark("planform.help.interval"))
        interval_row.addStretch()
        lay.addLayout(interval_row)
        threshold_row = QHBoxLayout()
        threshold_row.setContentsMargins(24, 0, 0, 0)
        threshold_row.addWidget(QLabel(tr("planform.thr.label")))
        threshold_row.addWidget(self.basic_threshold_combo)
        threshold_row.addWidget(self._help_mark("planform.help.threshold"))
        threshold_row.addStretch()
        lay.addLayout(threshold_row)
        lay.addWidget(self._with_help(self.freq_schedule_radio, "planform.help.schedule"))
        schedule_row = QHBoxLayout()
        schedule_row.setContentsMargins(24, 0, 0, 0)
        schedule_row.addWidget(QLabel(tr("planform.freq.schedule.every")))
        schedule_row.addWidget(self.basic_schedule_combo)
        schedule_row.addStretch()
        lay.addLayout(schedule_row)
        lay.addWidget(self.freq_manual_radio)
        lay.addWidget(self._indented(tr("planform.freq.manual.d")))
        lay.addWidget(self.freq_custom_radio)
        self.freq_custom_radio.setVisible(False)
        self.basic_quiet_check = QCheckBox(tr("planform.quiet", start="22:00", end="07:00"))
        self.basic_quiet_check.clicked.connect(lambda _=False: self._apply_freq_preset())
        lay.addWidget(self._with_help(self.basic_quiet_check, "planform.help.quiet"))
        basic_layout.addWidget(card)

        card, lay = self._card(tr("planform.card.keep"))
        self.basic_keep_combo = QComboBox()
        self.basic_keep_combo.addItem(tr("planform.keep.light"), "light")
        self.basic_keep_combo.addItem(tr("planform.keep.standard"), "standard")
        self.basic_keep_combo.addItem(tr("planform.keep.max"), "max")
        self.basic_keep_combo.activated.connect(lambda _=0: self._apply_keep_preset())
        lay.addWidget(self._with_help(self.basic_keep_combo, "planform.help.keep_basic"))
        lay.addWidget(self._indented(tr("planform.keep.d"), 0))
        basic_layout.addWidget(card)
        col.addWidget(self.basic_widget)

        # advanced page (original controls, same attribute names)
        self.advanced_widget = QWidget()
        adv = QVBoxLayout(self.advanced_widget)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.setSpacing(10)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("editor.mode.direct"), StorageMode.DIRECT.value)
        self.mode_combo.addItem(tr("editor.mode.zip"), StorageMode.ZIP.value)
        selected_mode = plan.storage_mode if plan else StorageMode.DIRECT.value
        index = self.mode_combo.findData(selected_mode)
        self.mode_combo.setCurrentIndex(index if index >= 0 else 0)
        self.keep_spin = QSpinBox()
        self.keep_spin.setRange(MIN_KEEP_LAST_N, MAX_KEEP_LAST_N)
        self.keep_spin.setValue(plan.keep_last_n if plan else DEFAULT_KEEP_LAST_N)
        self.note_edit = QTextEdit(plan.note if plan else "")
        self.note_edit.setFixedHeight(70)
        self.retention_mode_combo = QComboBox()
        self.retention_mode_combo.addItem(tr("editor.retention_count"), RETENTION_MODE_COUNT)
        self.retention_mode_combo.addItem(tr("editor.retention_time"), RETENTION_MODE_TIME)
        retention_mode = plan.retention_mode if plan else RETENTION_MODE_COUNT
        self.retention_mode_combo.setCurrentIndex(max(0, self.retention_mode_combo.findData(retention_mode)))
        self.retention_daily_spin = QSpinBox()
        self.retention_weekly_spin = QSpinBox()
        self.retention_monthly_spin = QSpinBox()
        for spin in (self.retention_daily_spin, self.retention_weekly_spin, self.retention_monthly_spin):
            spin.setRange(0, MAX_RETENTION_PERIODS)
        self.retention_daily_spin.setValue(plan.retention_daily if plan else DEFAULT_RETENTION_DAILY)
        self.retention_weekly_spin.setValue(plan.retention_weekly if plan else DEFAULT_RETENTION_WEEKLY)
        self.retention_monthly_spin.setValue(plan.retention_monthly if plan else DEFAULT_RETENTION_MONTHLY)

        card, lay = self._card(tr("planform.card.format"))
        form = QFormLayout()
        form.addRow(self._labeled(tr("editor.mode"), "planform.help.format"), self.mode_combo)
        lay.addLayout(form)
        self.mode_hint = QLabel()
        self.mode_hint.setProperty("role", "muted")
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setVisible(False)
        lay.addWidget(self.mode_hint)
        adv.addWidget(card)

        card, lay = self._card(tr("planform.card.retention"))
        form = QFormLayout()
        form.addRow(self._labeled(tr("editor.retention_mode"), "planform.help.retention_mode"), self.retention_mode_combo)
        form.addRow(self._labeled(tr("editor.keep"), "planform.help.keep"), self.keep_spin)
        form.addRow(self._labeled(tr("editor.retention_daily"), "planform.help.daily"), self.retention_daily_spin)
        form.addRow(self._labeled(tr("editor.retention_weekly"), "planform.help.weekly"), self.retention_weekly_spin)
        form.addRow(self._labeled(tr("editor.retention_monthly"), "planform.help.monthly"), self.retention_monthly_spin)
        lay.addLayout(form)
        adv.addWidget(card)

        automation_card, automation_layout = self._card(tr("editor.automation"))
        self.on_change_check = QCheckBox(tr("editor.on_change"))
        self.on_change_check.setChecked(plan.on_change_enabled if plan else False)
        automation_layout.addWidget(self._with_help(self.on_change_check, "planform.help.on_change"))
        self.debounce_spin = QSpinBox()
        self.debounce_spin.setRange(MIN_CHANGE_DEBOUNCE_SECONDS, MAX_CHANGE_DEBOUNCE_SECONDS)
        self.debounce_spin.setSuffix(tr("editor.seconds_suffix"))
        self.debounce_spin.setValue(plan.change_debounce_seconds if plan else DEFAULT_CHANGE_DEBOUNCE_SECONDS)
        self.min_interval_spin = QSpinBox()
        self.min_interval_spin.setRange(MIN_MIN_AUTO_INTERVAL_MINUTES, MAX_MIN_AUTO_INTERVAL_MINUTES)
        self.min_interval_spin.setSuffix(tr("editor.minutes_suffix"))
        self.min_interval_spin.setValue(plan.min_auto_interval_minutes if plan else DEFAULT_MIN_AUTO_INTERVAL_MINUTES)
        change_form = QFormLayout()
        self.threshold_combo = self._new_threshold_combo()
        self._select_threshold(self.threshold_combo, plan.change_threshold_bytes if plan else 0)
        change_form.addRow(self._labeled(tr("editor.debounce"), "planform.help.debounce"), self.debounce_spin)
        change_form.addRow(self._labeled(tr("editor.min_interval"), "planform.help.min_interval"), self.min_interval_spin)
        change_form.addRow(self._labeled(tr("planform.thr.label"), "planform.help.threshold"), self.threshold_combo)
        automation_layout.addLayout(change_form)
        self.on_schedule_check = QCheckBox(tr("editor.on_schedule"))
        self.on_schedule_check.setChecked(plan.on_schedule_enabled if plan else False)
        automation_layout.addWidget(self._with_help(self.on_schedule_check, "planform.help.schedule"))
        self.schedule_spin = QSpinBox()
        self.schedule_spin.setRange(MIN_SCHEDULE_INTERVAL_MINUTES, MAX_SCHEDULE_INTERVAL_MINUTES)
        self.schedule_spin.setSuffix(tr("editor.minutes_suffix"))
        self.schedule_spin.setValue(plan.schedule_interval_minutes if plan else DEFAULT_SCHEDULE_INTERVAL_MINUTES)
        schedule_form = QFormLayout()
        schedule_form.addRow(self._labeled(tr("editor.schedule_interval"), "planform.help.schedule_interval"), self.schedule_spin)
        automation_layout.addLayout(schedule_form)
        self.quiet_hours_check = QCheckBox(tr("editor.quiet_hours"))
        self.quiet_hours_check.setChecked(plan.quiet_hours_enabled if plan else False)
        automation_layout.addWidget(self._with_help(self.quiet_hours_check, "planform.help.quiet"))
        self.quiet_start_edit = QLineEdit(
            plan.quiet_hours_start_local if plan and plan.quiet_hours_start_local else "22:00")
        self.quiet_end_edit = QLineEdit(
            plan.quiet_hours_end_local if plan and plan.quiet_hours_end_local else "07:00")
        self.quiet_start_edit.setPlaceholderText("22:00")
        self.quiet_end_edit.setPlaceholderText("07:00")
        self.manual_quiet_check = QCheckBox(tr("editor.manual_quiet"))
        self.manual_quiet_check.setChecked(plan.manual_allowed_quiet_hours if plan else True)
        quiet_form = QFormLayout()
        quiet_form.addRow(tr("editor.quiet_from"), self.quiet_start_edit)
        quiet_form.addRow(tr("editor.quiet_to"), self.quiet_end_edit)
        quiet_form.addRow("", self._with_help(self.manual_quiet_check, "planform.help.manual_quiet"))
        automation_layout.addLayout(quiet_form)
        self.automation_hint = QLabel()
        self.automation_hint.setProperty("role", "muted")
        self.automation_hint.setWordWrap(True)
        automation_layout.addWidget(self.automation_hint)
        adv.addWidget(automation_card)

        card, lay = self._card(tr("planform.card.note"))
        lay.addWidget(self.note_edit)
        adv.addWidget(card)
        col.addWidget(self.advanced_widget)
        col.addStretch(1)

        # summary panel
        side = QFrame()
        side.setProperty("role", "card")
        side.setFixedWidth(270)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(14, 12, 14, 12)
        side_layout.setSpacing(8)
        side_title = QLabel(tr("planform.sum.title"))
        side_title.setProperty("role", "subtitle")
        side_layout.addWidget(side_title)
        self.summary_size = QLabel()
        self.summary_free = QLabel()
        self.summary_freq = QLabel()
        self.summary_keep = QLabel()
        self.summary_warning = QLabel()
        self.summary_warning.setStyleSheet("color:#ef6974;")
        for label in (self.summary_size, self.summary_free, self.summary_freq, self.summary_keep, self.summary_warning):
            label.setWordWrap(True)
            side_layout.addWidget(label)
        side_layout.addStretch(1)
        body.addWidget(side)

        # footer
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton(tr("common.cancel"))
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.create_button: QPushButton | None = None
        self.save_button = QPushButton()
        self.save_button.setProperty("primary", True)
        if plan is None:
            self.create_button = QPushButton(tr("planform.btn.create"))
            self.create_button.clicked.connect(lambda _=False: self.save(False))
            actions.addWidget(self.create_button)
            self.save_button.setText(tr("planform.btn.create_run"))
            self.save_button.clicked.connect(lambda _=False: self.save(True))
        else:
            self.save_button.setText(tr("common.save"))
            self.save_button.clicked.connect(lambda _=False: self.save(False))
        actions.addWidget(self.save_button)
        root.addLayout(actions)

        # wiring
        self.mode_combo.currentIndexChanged.connect(self.update_mode_hint)
        self.update_mode_hint()
        if plan is not None and plan.snapshot_count > 0:
            self.mode_combo.setEnabled(False)
            self.mode_hint.setText(tr("editor.mode.locked"))
            self.mode_hint.setVisible(True)
        self.retention_mode_combo.currentIndexChanged.connect(self.update_retention_controls)
        self.update_retention_controls()
        for check in (self.on_change_check, self.on_schedule_check, self.quiet_hours_check):
            check.toggled.connect(self.update_automation_controls)
        archived = plan is not None and plan.state == PlanState.ARCHIVED.value
        if archived:
            self.disable_automation_controls()
            self.automation_hint.setText(tr("editor.archived_hint"))
        else:
            self.update_automation_controls()

        if plan is None:
            self.freq_auto_radio.setChecked(True)
            # "Standard" keeps one version per day, so older versions of the same day silently disappear;
            # a new plan starts with the "last N versions" policy instead.
            self.basic_keep_combo.setCurrentIndex(self.basic_keep_combo.findData("max"))
            self._select_or_add(self.basic_interval_combo, DEFAULT_MIN_AUTO_INTERVAL_MINUTES)
            self._apply_freq_preset()
            self._apply_keep_preset()
        else:
            self._sync_basic_from_advanced()
        if archived:
            for widget in (self.freq_auto_radio, self.freq_schedule_radio, self.freq_manual_radio,
                           self.freq_custom_radio, self.basic_interval_combo, self.basic_schedule_combo,
                           self.basic_quiet_check, self.basic_threshold_combo):
                widget.setEnabled(False)

        self.basic_button.clicked.connect(lambda _=False: self.set_advanced(False))
        self.advanced_button.clicked.connect(lambda _=False: self.set_advanced(True))
        advanced = start_advanced if start_advanced is not None else (
            True if plan is not None else PlanEditor._last_advanced)
        self.set_advanced(bool(advanced), remember=False)

        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.setInterval(450)
        self._scan_timer.timeout.connect(self._begin_scan)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(250)
        self._poll_timer.timeout.connect(self._poll_scan)
        self.source_edit.textChanged.connect(self._on_source_changed)
        self.storage_edit.textChanged.connect(self._refresh_summary)
        for spin in (self.keep_spin, self.retention_daily_spin, self.retention_weekly_spin,
                     self.retention_monthly_spin, self.debounce_spin, self.min_interval_spin, self.schedule_spin):
            spin.valueChanged.connect(self._refresh_summary)
        self.retention_mode_combo.currentIndexChanged.connect(self._refresh_summary)
        self.threshold_combo.currentIndexChanged.connect(self._refresh_summary)
        for check in (self.on_change_check, self.on_schedule_check):
            check.toggled.connect(self._refresh_summary)
        self._path_drop = PathDropTarget(self, controller.i18n, "source", self._apply_source_drop)
        self._on_source_changed()
        self._wheel_guard = install_wheel_guard(self, scroll)

    # ---- helpers ----
    @staticmethod
    def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setProperty("role", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        label = QLabel(title)
        label.setProperty("role", "subtitle")
        lay.addWidget(label)
        return card, lay

    def _help_mark(self, key: str) -> HelpMark:
        return HelpMark(self.controller.i18n.tr(key))

    def _labeled(self, text: str, help_key: str) -> QWidget:
        box = QWidget()
        box.setObjectName("helpRow")
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(QLabel(text))
        row.addWidget(self._help_mark(help_key))
        row.addStretch(1)
        return box

    def _with_help(self, widget: QWidget, help_key: str) -> QWidget:
        box = QWidget()
        box.setObjectName("helpRow")
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(widget)
        row.addWidget(self._help_mark(help_key))
        row.addStretch(1)
        return box

    def _threshold_text(self, value: int) -> str:
        tr = self.controller.i18n.tr
        if value <= 0:
            return tr("planform.thr.any")
        if value % (1 << 30) == 0:
            return tr("planform.thr.gb", n=value >> 30)
        if value % (1 << 20) == 0:
            return tr("planform.thr.mb", n=value >> 20)
        return tr("planform.thr.kb", n=max(1, round(value / 1024)))

    def _new_threshold_combo(self) -> QComboBox:
        combo = QComboBox()
        for value in CHANGE_THRESHOLD_STEPS:
            combo.addItem(self._threshold_text(value), value)
        return combo

    def _select_threshold(self, combo: QComboBox, value: int) -> None:
        index = combo.findData(int(value))
        if index < 0:
            combo.addItem(self._threshold_text(int(value)), int(value))
            index = combo.count() - 1
        combo.setCurrentIndex(index)

    def _apply_threshold_from_basic(self) -> None:
        self._select_threshold(self.threshold_combo, int(self.basic_threshold_combo.currentData()))
        self._refresh_summary()

    @staticmethod
    def _indented(text: str, left: int = 24) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "muted")
        label.setWordWrap(True)
        label.setContentsMargins(left, 0, 0, 0)
        return label

    def _minutes_text(self, minutes: int) -> str:
        tr = self.controller.i18n.tr
        if minutes % 10080 == 0:
            return tr("planform.unit.week", n=minutes // 10080)
        if minutes % 1440 == 0:
            return tr("planform.unit.day", n=minutes // 1440)
        if minutes % 60 == 0:
            return tr("planform.unit.hour", n=minutes // 60)
        return tr("planform.unit.min", n=minutes)

    def _select_or_add(self, combo: QComboBox, value: int) -> None:
        index = combo.findData(value)
        if index < 0:
            combo.addItem(self._minutes_text(value), value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)

    def set_advanced(self, advanced: bool, remember: bool = True) -> None:
        tr = self.controller.i18n.tr
        if not advanced:
            self._sync_basic_from_advanced()
        self.is_advanced = advanced
        if remember and self.plan is None:
            PlanEditor._last_advanced = advanced
        self.basic_button.setChecked(not advanced)
        self.advanced_button.setChecked(advanced)
        self.basic_widget.setVisible(not advanced)
        self.advanced_widget.setVisible(advanced)
        self.storage_basic_note.setVisible(not advanced)
        self.switch_hint.setText(tr("planform.switch.advanced") if advanced else tr("planform.switch.basic"))
        self._refresh_summary()

    # ---- presets ----
    def _apply_freq_preset(self) -> None:
        if self.freq_auto_radio.isChecked():
            self.on_change_check.setChecked(True)
            self.on_schedule_check.setChecked(False)
            self.debounce_spin.setValue(DEFAULT_CHANGE_DEBOUNCE_SECONDS)
            self.min_interval_spin.setValue(int(self.basic_interval_combo.currentData()))
            self._select_threshold(self.threshold_combo, int(self.basic_threshold_combo.currentData()))
        elif self.freq_schedule_radio.isChecked():
            self.on_change_check.setChecked(False)
            self.on_schedule_check.setChecked(True)
            self.schedule_spin.setValue(int(self.basic_schedule_combo.currentData()))
        elif self.freq_manual_radio.isChecked():
            self.on_change_check.setChecked(False)
            self.on_schedule_check.setChecked(False)
        if not self.freq_custom_radio.isChecked():
            self.quiet_hours_check.setChecked(self.basic_quiet_check.isChecked())
        self.basic_quiet_check.setEnabled(not self.freq_manual_radio.isChecked())
        self.update_automation_controls()
        self._refresh_summary()

    def _apply_keep_preset(self) -> None:
        key = self.basic_keep_combo.currentData()
        if key == "light":
            self.retention_mode_combo.setCurrentIndex(self.retention_mode_combo.findData(RETENTION_MODE_COUNT))
            self.keep_spin.setValue(self.KEEP_LIGHT)
        elif key == "max":
            self.retention_mode_combo.setCurrentIndex(self.retention_mode_combo.findData(RETENTION_MODE_COUNT))
            self.keep_spin.setValue(self.KEEP_MAX)
        elif key == "standard":
            self.retention_mode_combo.setCurrentIndex(self.retention_mode_combo.findData(RETENTION_MODE_TIME))
            self.retention_daily_spin.setValue(DEFAULT_RETENTION_DAILY)
            self.retention_weekly_spin.setValue(DEFAULT_RETENTION_WEEKLY)
            self.retention_monthly_spin.setValue(DEFAULT_RETENTION_MONTHLY)
        self.update_retention_controls()
        self._refresh_summary()

    def _sync_basic_from_advanced(self) -> None:
        tr = self.controller.i18n.tr
        on_change = self.on_change_check.isChecked()
        on_schedule = self.on_schedule_check.isChecked()
        if on_change and not on_schedule:
            radio = self.freq_auto_radio
        elif on_schedule and not on_change:
            radio = self.freq_schedule_radio
        elif not on_change and not on_schedule:
            radio = self.freq_manual_radio
        else:
            radio = self.freq_custom_radio
        self.freq_custom_radio.setVisible(radio is self.freq_custom_radio)
        radio.setChecked(True)
        self._select_or_add(self.basic_interval_combo, self.min_interval_spin.value())
        self._select_threshold(self.basic_threshold_combo, int(self.threshold_combo.currentData() or 0))
        self.basic_threshold_combo.setEnabled(radio is self.freq_auto_radio)
        self._select_or_add(self.basic_schedule_combo, self.schedule_spin.value())
        self.basic_quiet_check.setChecked(self.quiet_hours_check.isChecked())
        self.basic_quiet_check.setText(tr(
            "planform.quiet",
            start=self.quiet_start_edit.text().strip() or "22:00",
            end=self.quiet_end_edit.text().strip() or "07:00"))
        self.basic_quiet_check.setEnabled(radio is not self.freq_manual_radio)
        mode = self.retention_mode_combo.currentData()
        if mode == RETENTION_MODE_COUNT and self.keep_spin.value() == self.KEEP_LIGHT:
            key = "light"
        elif mode == RETENTION_MODE_COUNT and self.keep_spin.value() == self.KEEP_MAX:
            key = "max"
        elif (mode == RETENTION_MODE_TIME and self.retention_daily_spin.value() == DEFAULT_RETENTION_DAILY
              and self.retention_weekly_spin.value() == DEFAULT_RETENTION_WEEKLY
              and self.retention_monthly_spin.value() == DEFAULT_RETENTION_MONTHLY):
            key = "standard"
        else:
            key = "custom"
        custom_index = self.basic_keep_combo.findData("custom")
        if key == "custom" and custom_index < 0:
            self.basic_keep_combo.addItem(tr("planform.keep.custom"), "custom")
        elif key != "custom" and custom_index >= 0:
            self.basic_keep_combo.removeItem(custom_index)
        self.basic_keep_combo.setCurrentIndex(self.basic_keep_combo.findData(key))

    # ---- source / storage ----
    def _apply_source_drop(self, path: Path) -> None:
        self.source_edit.setText(str(path))

    def _derived_name(self) -> str:
        text = self.source_edit.text().strip().rstrip("\\/")
        if not text:
            return ""
        path = Path(text)
        return path.stem if path.suffix and not path.is_dir() else (path.name or text)

    def pick_source_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, self.controller.i18n.tr("editor.choose_folder"))
        if selected:
            self.source_edit.setText(selected)

    def pick_source_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, self.controller.i18n.tr("editor.choose_file"))
        if selected:
            self.source_edit.setText(selected)

    def _on_source_changed(self) -> None:
        derived = self._derived_name()
        self.name_edit.setPlaceholderText(
            self.controller.i18n.tr("planform.name_auto", name=derived) if derived
            else self.controller.i18n.tr("planform.name_auto_empty"))
        self._scan_stop.set()
        self._scan_state = {"token": self._scan_state["token"] + 1, "done": False, "ok": True,
                            "bytes": 0, "files": 0, "partial": False}
        self._poll_timer.stop()
        if self.source_edit.text().strip():
            self._scan_timer.start()
        else:
            self._scan_timer.stop()
            self._scan_state["done"] = True
        self._refresh_summary()

    def _begin_scan(self) -> None:
        text = self.source_edit.text().strip()
        if not text:
            return
        self._scan_stop = threading.Event()
        token = self._scan_state["token"]
        self._scan_state = {"token": token, "done": False, "ok": True, "bytes": 0, "files": 0, "partial": False}
        state, stop = self._scan_state, self._scan_stop
        limit_files, limit_seconds = self.SCAN_MAX_FILES, self.SCAN_MAX_SECONDS

        def worker() -> None:
            total = files = 0
            partial = False
            ok = True
            try:
                root_path = Path(text)
                if root_path.is_file():
                    total, files = root_path.stat().st_size, 1
                elif root_path.is_dir():
                    stack = [str(root_path)]
                    started = time.monotonic()
                    while stack and not partial:
                        if stop.is_set():
                            return
                        current = stack.pop()
                        try:
                            with os.scandir(current) as entries:
                                for entry in entries:
                                    if stop.is_set():
                                        return
                                    try:
                                        if entry.is_symlink():
                                            continue
                                        if entry.is_dir(follow_symlinks=False):
                                            stack.append(entry.path)
                                        elif entry.is_file(follow_symlinks=False):
                                            total += entry.stat(follow_symlinks=False).st_size
                                            files += 1
                                    except OSError:
                                        pass
                                    if files >= limit_files or time.monotonic() - started > limit_seconds:
                                        partial = True
                                        break
                        except OSError:
                            pass
                else:
                    ok = False
            except OSError:
                ok = False
            state.update(bytes=total, files=files, partial=partial, ok=ok, done=True)

        threading.Thread(target=worker, daemon=True).start()
        self._poll_timer.start()

    def _poll_scan(self) -> None:
        self._refresh_summary()
        if self._scan_state.get("done"):
            self._poll_timer.stop()

    @staticmethod
    def _free_space(text: str) -> int | None:
        text = text.strip()
        if not text:
            return None
        path = Path(text)
        if not path.is_absolute():
            return None
        try:
            while True:
                if path.exists():
                    return shutil.disk_usage(path).free
                if path.parent == path:
                    return None
                path = path.parent
        except OSError:
            return None

    @staticmethod
    def _overlap(source: str, storage: str) -> str | None:
        if not source.strip() or not storage.strip():
            return None
        if not Path(source.strip()).is_absolute() or not Path(storage.strip()).is_absolute():
            return None
        try:
            a = os.path.normcase(os.path.realpath(source.strip())).rstrip("\\/")
            b = os.path.normcase(os.path.realpath(storage.strip())).rstrip("\\/")
        except (OSError, ValueError):
            return None
        if a == b or b.startswith(a + os.sep):
            return "storage_inside"
        if a.startswith(b + os.sep):
            return "source_inside"
        return None

    def _cached_free(self, storage: str) -> int | None:
        cached = getattr(self, "_free_cache", None)
        now = time.monotonic()
        if cached and cached[0] == storage and now - cached[1] < 5.0:
            return cached[2]
        value = self._free_space(storage)
        self._free_cache = (storage, now, value)
        return value

    def _cached_overlap(self, source: str, storage: str) -> str | None:
        key = (source, storage)
        cached = getattr(self, "_overlap_cache", None)
        if cached and cached[0] == key:
            return cached[1]
        value = self._overlap(source, storage)
        self._overlap_cache = (key, value)
        return value

    def _refresh_summary(self) -> None:
        tr = self.controller.i18n.tr
        source = self.source_edit.text().strip()
        storage = self.storage_edit.text().strip()
        state = self._scan_state
        warnings: list[str] = []
        self.source_hint.setText("")
        if source and state.get("done") and not state.get("ok"):
            self.source_hint.setText(tr("planform.warn.source_missing"))
            self.source_hint.setStyleSheet("color:#ef6974;")
        if not source:
            self.summary_size.setText(tr("planform.sum.none"))
        elif not state.get("done"):
            self.summary_size.setText(tr("planform.sum.scanning"))
        elif not state.get("ok"):
            self.summary_size.setText(tr("planform.sum.none"))
        elif state.get("partial"):
            self.summary_size.setText(tr("planform.sum.size.more", size=fmt_size(state["bytes"]), files=state["files"]))
        else:
            self.summary_size.setText(tr("planform.sum.size", size=fmt_size(state["bytes"]), files=state["files"]))
        free = self._cached_free(storage)
        self.summary_free.setText(
            tr("planform.sum.free", free=fmt_size(free)) if free is not None else tr("planform.sum.free.unknown"))
        overlap = self._cached_overlap(source, storage)
        self.storage_hint.setText("")
        if overlap == "storage_inside":
            warnings.append(tr("planform.warn.inside"))
        elif overlap == "source_inside":
            warnings.append(tr("planform.warn.source_in_storage"))
        if self.storage_hint.text() == "" and overlap:
            self.storage_hint.setText(warnings[-1])
            self.storage_hint.setStyleSheet("color:#ef6974;")
        if free is not None and state.get("done") and state.get("ok"):
            need = int(state["bytes"] + max(FREE_SPACE_MIN_RESERVE_BYTES, state["bytes"] * FREE_SPACE_RESERVE_RATIO))
            if (state["bytes"] > free) or (not state.get("partial") and need > free):
                warnings.append(tr("planform.warn.space"))
        on_change = self.on_change_check.isChecked()
        on_schedule = self.on_schedule_check.isChecked()
        every = self._minutes_text(self.min_interval_spin.value())
        sched = self._minutes_text(self.schedule_spin.value())
        if on_change and on_schedule:
            freq = tr("planform.sum.freq.both", every=every, sched=sched)
        elif on_change:
            freq = tr("planform.sum.freq.auto", every=every)
        elif on_schedule:
            freq = tr("planform.sum.freq.schedule", every=sched)
        else:
            freq = tr("planform.sum.freq.manual")
        if on_change and int(self.threshold_combo.currentData() or 0) > 0:
            freq += "\n" + tr("planform.threshold_summary.threshold", size=self._threshold_text(int(self.threshold_combo.currentData())))
        self.summary_freq.setText(freq)
        if self.retention_mode_combo.currentData() == RETENTION_MODE_TIME:
            keep = tr("planform.sum.keep.time", d=self.retention_daily_spin.value(),
                      w=self.retention_weekly_spin.value(), m=self.retention_monthly_spin.value())
            keep += "\n" + tr("planform.sum.keep.count", n=self.keep_spin.value())
        else:
            keep = tr("planform.sum.keep.count", n=self.keep_spin.value())
        self.summary_keep.setText(keep)
        self.summary_warning.setText("\n".join(warnings))
        ready = bool(source) and bool(storage) and overlap is None
        self.save_button.setEnabled(ready)
        if self.create_button is not None:
            self.create_button.setEnabled(ready)

    def done(self, result: int) -> None:
        self._scan_stop.set()
        self._scan_timer.stop()
        self._poll_timer.stop()
        super().done(result)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_drop_overlay"):
            self._drop_overlay.setGeometry(self.rect())

    def update_mode_hint(self) -> None:
        tr = self.controller.i18n.tr
        key = (
            "editor.mode.direct_hint"
            if self.mode_combo.currentData()
            == StorageMode.DIRECT.value
            else "editor.mode.zip_hint"
        )
        self.mode_hint.setText(tr(key))
        self.mode_hint.setVisible(False)

    def disable_automation_controls(self) -> None:
        for widget in (
            self.on_change_check,
            self.debounce_spin,
            self.min_interval_spin,
            self.threshold_combo,
            self.on_schedule_check,
            self.schedule_spin,
            self.quiet_hours_check,
            self.quiet_start_edit,
            self.quiet_end_edit,
            self.manual_quiet_check,
        ):
            widget.setEnabled(False)

    def update_automation_controls(self) -> None:
        tr = self.controller.i18n.tr
        on_change = self.on_change_check.isChecked()
        on_schedule = self.on_schedule_check.isChecked()
        quiet_hours = self.quiet_hours_check.isChecked()

        self.debounce_spin.setEnabled(on_change)
        self.threshold_combo.setEnabled(on_change)
        self.basic_threshold_combo.setEnabled(on_change and not self.on_schedule_check.isChecked() and self.freq_auto_radio.isChecked())
        self.min_interval_spin.setEnabled(
            on_change or on_schedule
        )
        self.schedule_spin.setEnabled(on_schedule)
        self.quiet_start_edit.setEnabled(quiet_hours)
        self.quiet_end_edit.setEnabled(quiet_hours)
        self.manual_quiet_check.setEnabled(quiet_hours)

        modes: list[str] = []
        if on_change:
            modes.append(tr("editor.auto_change"))
        if on_schedule:
            modes.append(tr("editor.auto_schedule"))

        if not modes:
            self.automation_hint.setText(tr("editor.auto_off"))
            return

        text = tr(
            "editor.auto_on",
            modes=tr("editor.auto_join").join(modes),
        )
        if quiet_hours:
            text += " " + tr("editor.auto_quiet")
        self.automation_hint.setText(text)

    def pick_source(self) -> None:
        tr = self.controller.i18n.tr
        selected, _ = QFileDialog.getOpenFileName(
            self, tr("editor.choose_file")
        )
        if not selected:
            selected = QFileDialog.getExistingDirectory(
                self, tr("editor.choose_folder")
            )
        if selected:
            self.source_edit.setText(selected)
            if not self.name_edit.text().strip():
                path = Path(selected)
                self.name_edit.setText(
                    path.stem if path.suffix else path.name
                )

    def _load_storage_history(self) -> None:
        tr = self.controller.i18n.tr
        combo = self.storage_history_combo
        combo.blockSignals(True)
        combo.clear()
        try:
            recent, popular = self.controller.repo.storage_root_history()
        except Exception:
            logging.exception("Storage history unavailable")
            recent, popular = [], []
        if not recent and not popular:
            combo.addItem(tr("folders.history.empty"), None)
            combo.setEnabled(False)
        else:
            combo.addItem(tr("folders.history.placeholder"), None)
            for title_key, paths in (("folders.history.recent", recent), ("folders.history.popular", popular)):
                if not paths:
                    continue
                combo.addItem(tr(title_key), None)
                combo.model().item(combo.count() - 1).setEnabled(False)
                for path in paths:
                    combo.addItem(path, path)
                    combo.setItemData(combo.count() - 1, path, Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _on_storage_history_picked(self, index: int) -> None:
        path = self.storage_history_combo.itemData(index)
        if path:
            self.storage_edit.setText(str(path))
        self.storage_history_combo.setCurrentIndex(0)

    def pick_storage(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            self.controller.i18n.tr(
                "editor.choose_storage"
            ),
        )
        if selected:
            self.storage_edit.setText(selected)

    def update_retention_controls(self) -> None:
        by_time = self.retention_mode_combo.currentData() == RETENTION_MODE_TIME
        self.keep_spin.setEnabled(True)  # in time mode it is the number of newest versions that are always kept
        self.retention_daily_spin.setEnabled(by_time)
        self.retention_weekly_spin.setEnabled(by_time)
        self.retention_monthly_spin.setEnabled(by_time)

    def build_retention_policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            str(self.retention_mode_combo.currentData()),
            self.keep_spin.value(),
            self.retention_daily_spin.value(),
            self.retention_weekly_spin.value(),
            self.retention_monthly_spin.value(),
        )

    def build_trigger_policy(self) -> TriggerPolicy:
        return TriggerPolicy(
            on_change_enabled=self.on_change_check.isChecked(),
            change_debounce_seconds=self.debounce_spin.value(),
            min_auto_interval_minutes=self.min_interval_spin.value(),
            on_schedule_enabled=self.on_schedule_check.isChecked(),
            schedule_interval_minutes=self.schedule_spin.value(),
            quiet_hours_enabled=self.quiet_hours_check.isChecked(),
            quiet_hours_start_local=(
                self.quiet_start_edit.text()
                if self.quiet_hours_check.isChecked()
                else None
            ),
            quiet_hours_end_local=(
                self.quiet_end_edit.text()
                if self.quiet_hours_check.isChecked()
                else None
            ),
            manual_allowed_quiet_hours=(
                self.manual_quiet_check.isChecked()
            ),
            change_threshold_bytes=int(self.threshold_combo.currentData() or 0),
        )

    def save(self, start_first_backup: bool = False) -> None:
        tr = self.controller.i18n.tr
        try:
            retention = self.controller.repo.validate_retention_policy(self.build_retention_policy())
            preview_token: str | None = None
            preview: RetentionPreview | None = None
            if self.plan is not None:
                old = RetentionPolicy(
                    self.plan.retention_mode,self.plan.keep_last_n,self.plan.retention_daily,
                    self.plan.retention_weekly,self.plan.retention_monthly)
                if old != retention or (
                    retention.mode == RETENTION_MODE_TIME and self.controller.retention_clock_blocked()
                ):
                    preview = self.controller.retention_change_preview(self.plan.id,retention)
                    numbers = ", ".join(str(value) for value in preview.delete_sequence_numbers) or tr("editor.retention_none")
                    question = tr("editor.retention_preview", count=preview.delete_count, versions=numbers)
                    if preview.clock_blocked:
                        question += "\n\n" + tr("editor.retention_clock_warning")
                    answer = QMessageBox.question(
                        self,tr("editor.retention_preview_title"),question,
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No)
                    if answer != QMessageBox.StandardButton.Yes:
                        return
                    preview_token = preview.token
            saved_id = self.controller.save_plan_with_policy(
                self.plan.id if self.plan else None,self.name_edit.text().strip() or self._derived_name(),self.note_edit.toPlainText(),
                self.source_edit.text(),self.storage_edit.text(),self.keep_spin.value(),
                str(self.mode_combo.currentData()),self.build_trigger_policy(),retention,preview_token)
            if preview is not None and preview.delete_count:
                current_preview = self.controller.retention_change_preview(saved_id,retention)
                if current_preview.delete_snapshot_ids != preview.delete_snapshot_ids:
                    raise AppError(
                        "RETENTION_PREVIEW_STALE",
                        "The history changed. Review the deletion list again.",
                    )
                self.controller.queue_apply_retention(saved_id,current_preview.token)
            self.created_plan_id = saved_id
            self.start_first_backup = bool(start_first_backup and self.plan is None)
            self.accept()
        except AppError as exc:
            QMessageBox.warning(self,tr("editor.save_error_title"),self.controller.i18n.error_message(exc))
        except Exception:
            logging.exception("Plan save failure")
            QMessageBox.critical(self,tr("editor.unexpected_error_title"),tr("editor.unexpected_error"))



class HealthCenterDialog(QDialog):
    FILTERS = {"all", "error", "warning"}

    def __init__(
        self,
        controller: "AppController",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.filter_name = "all"
        tr = controller.i18n.tr

        self.setWindowTitle(tr("app.health_center"))
        self.resize(820, 600)
        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setStyleSheet("font-size:16px;font-weight:700;")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        controls = QHBoxLayout()
        self.all_button = QPushButton(tr("health.all"))
        self.errors_button = QPushButton(tr("health.errors"))
        self.warnings_button = QPushButton(tr("health.warnings"))

        for button, value in (
            (self.all_button, "all"),
            (self.errors_button, "error"),
            (self.warnings_button, "warning"),
        ):
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, name=value:
                self.set_filter(name)
            )
            controls.addWidget(button)

        controls.addStretch()
        layout.addLayout(controls)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.items_layout = QVBoxLayout(self.container)
        self.items_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.items_layout.setSpacing(8)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        close = QPushButton(tr("common.close"))
        close.setProperty("primary", True)
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.set_filter("all")

    def set_filter(self, filter_name: str) -> None:
        if filter_name not in self.FILTERS:
            raise AppError(
                "HEALTH_FILTER_INVALID",
                f"Unsupported Health Center filter: {filter_name}",
            )

        self.filter_name = filter_name
        self.all_button.setChecked(filter_name == "all")
        self.errors_button.setChecked(filter_name == "error")
        self.warnings_button.setChecked(filter_name == "warning")
        self.refresh()

    def refresh(self) -> None:
        clear_layout_recursively(self.items_layout)
        tr = self.controller.i18n.tr
        issues = self.controller.repo.list_active_issues()
        error_count = sum(1 for item in issues if item.severity in {IssueSeverity.ERROR.value, IssueSeverity.CRITICAL.value})
        warning_count = sum(1 for item in issues if item.severity == IssueSeverity.WARNING.value)
        self.summary_label.setText(
            tr("healthcenter.summary", errors=error_count, warnings=warning_count) if issues else tr("healthcenter.clean"))

        if self.filter_name == "error":
            issues = [
                issue for issue in issues
                if issue.severity in {
                    IssueSeverity.ERROR.value,
                    IssueSeverity.CRITICAL.value,
                }
            ]
        elif self.filter_name == "warning":
            issues = [
                issue for issue in issues
                if issue.severity == IssueSeverity.WARNING.value
            ]

        if not issues:
            self.items_layout.addWidget(
                EmptyState(
                    tr("health.empty_title"),
                    tr("health.empty_detail"),
                )
            )
            return

        colors = {
            IssueSeverity.WARNING.value: "#f3bd4f",
            IssueSeverity.ERROR.value: "#ef6974",
            IssueSeverity.CRITICAL.value: "#ef6974",
        }
        i18n = self.controller.i18n
        for issue in issues:
            color = colors.get(issue.severity, "#9aa8bc")
            card = QFrame()
            card.setProperty("role", "card")
            card.setStyleSheet(f'QFrame[role="card"] {{ border-left: 4px solid {color}; }}')
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 12, 14, 12)
            card_layout.setSpacing(6)

            header = QHBoxLayout()
            title = QLabel(i18n.issue_title(issue.code))
            title.setProperty("role", "subtitle")
            severity_key = "severity.warning" if issue.severity == IssueSeverity.WARNING.value else "severity.error"
            severity = QLabel(tr(severity_key))
            severity.setStyleSheet(f"color:{color};font-weight:700;background:transparent;border:none;")
            timestamp = QLabel(fmt_time(issue.last_seen_at_utc_ms))
            timestamp.setProperty("role", "muted")
            header.addWidget(title)
            header.addWidget(severity)
            header.addStretch()
            header.addWidget(timestamp)
            card_layout.addLayout(header)

            if issue.plan_id:
                try:
                    set_name = self.controller.repo.plan_details(issue.plan_id).display_name
                except AppError:
                    set_name = ""
                if set_name:
                    where = QLabel(tr("healthcenter.where", name=set_name))
                    where.setProperty("role", "muted")
                    card_layout.addWidget(where)

            raw_codes = {
                "RESTORE_RECOVERY_REVIEW_REQUIRED", "PLAN_DELETE_INCOMPLETE", "SOURCE_LOCKED",
                "SOURCE_IDENTITY_UNCONFIRMED", "SOURCE_IDENTITY_MISMATCH",
                "SOURCE_IDENTITY_AMBIGUOUS", "SOURCE_IDENTITY_UNAVAILABLE", "SOURCE_NOT_FOUND",
            }
            use_raw = issue.code in raw_codes and i18n.language == Language.RUSSIAN.value and not i18n.has_key(f"issue.detail.{issue.code}")
            happened = QLabel(tr("healthcenter.what", text=issue.detail if use_raw else i18n.issue_detail(issue.code)))
            happened.setWordWrap(True)
            card_layout.addWidget(happened)

            fix_key = f"issue.fix.{issue.code}"
            todo = QLabel(tr("healthcenter.todo", text=tr(fix_key) if i18n.has_key(fix_key) else tr("issue.fix.generic")))
            todo.setWordWrap(True)
            todo.setStyleSheet("font-weight:600;")
            card_layout.addWidget(todo)

            code_label = QLabel(tr("healthcenter.code", code=issue.code))
            code_label.setProperty("role", "muted")
            code_label.setStyleSheet("font-size:11px;")
            card_layout.addWidget(code_label)

            actions = QHBoxLayout()
            actions.addStretch()
            for label, callback, primary in self.issue_actions(issue):
                button = QPushButton(label)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setMinimumHeight(32)
                if primary:
                    button.setProperty("primary", True)
                button.clicked.connect(lambda _checked=False, cb=callback: self.run_fix(cb))
                actions.addWidget(button)

            dismiss = QPushButton(tr("healthcenter.hide"))
            dismiss.setProperty("role", "ghost")
            dismiss.setMinimumHeight(32)
            dismiss.setToolTip(tr("healthcenter.hide_tip"))
            dismiss.clicked.connect(
                lambda _checked=False, key=issue.key:
                self.dismiss_issue(key)
            )
            actions.addWidget(dismiss)
            card_layout.addLayout(actions)
            self.items_layout.addWidget(card)

    def issue_actions(self, issue) -> list:
        tr = self.controller.i18n.tr
        window = self.controller.main_window
        plan_id = issue.plan_id
        if window is None or not plan_id:
            return []
        code = issue.code
        result: list = []
        retry = (tr("fixbtn.retry"), lambda: window.queue_backup(plan_id), True)
        new_copy = (tr("fixbtn.new_copy"), lambda: window.queue_backup(plan_id), True)
        open_set = (tr("fixbtn.open"), lambda: self.open_plan(plan_id), False)
        edit_set = (tr("fixbtn.edit"), lambda: window.edit_plan(plan_id), False)
        needs_recovery = self.controller.repo.needs_recovery_for_plan(plan_id)
        if code in {"OPERATION_NEEDS_RECOVERY", "PLAN_DELETE_INCOMPLETE", "RESTORE_RECOVERY_REVIEW_REQUIRED"}:
            if needs_recovery:
                deleting = self.controller.repo.plan_deletion_job(plan_id) is not None
                result.append((tr("fixbtn.continue_delete" if deleting else "fixbtn.recover"),
                               lambda: window.start_recovery(plan_id), True))
            result.append(open_set)
        elif code.startswith("SOURCE_IDENTITY"):
            if self.controller.source_confirmation_required(plan_id):
                result.append((tr("fixbtn.confirm"), lambda: window.confirm_source_identity(plan_id), True))
            result.append(edit_set)
        elif code in {"SOURCE_NOT_FOUND", "SOURCE_ACCESS_DENIED", "SOURCE_KIND_MISMATCH", "SOURCE_PROBE_ERROR"}:
            result.extend([edit_set, retry] if code == "SOURCE_KIND_MISMATCH" else [retry, edit_set])
        elif code in {"SOURCE_LOCKED", "SOURCE_CHANGED_DURING_READ"} or code.startswith("STORAGE_"):
            result.extend([retry, open_set])
        elif code in {"SNAPSHOT_CORRUPTED", "VERIFY_FAILED"}:
            result.extend([new_copy, open_set])
        else:
            result.append(open_set)
        return result

    def run_fix(self, callback) -> None:
        callback()
        self.refresh()

    def dismiss_issue(self, dedupe_key: str) -> None:
        try:
            self.controller.dismiss_issue(dedupe_key)
            self.refresh()
            if self.controller.main_window is not None:
                self.controller.main_window.refresh()
        except AppError as exc:
            QMessageBox.warning(
                self,
                self.controller.i18n.tr("health.dismiss_error_title"),
                self.controller.i18n.error_message(exc),
            )

    def open_plan(self, plan_id: str) -> None:
        if self.controller.main_window is not None:
            self.controller.main_window.select_plan_by_id(plan_id)
        self.accept()


def legal_file_candidates(name: str) -> list[Path]:
    """Places where the 'legal' folder (license texts) may be: next to the script, in the bundle, next to the exe."""
    folders = [Path(__file__).resolve().parent]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        folders.append(Path(bundle))
    if getattr(sys, "frozen", False):
        folders.append(Path(sys.executable).resolve().parent)
    result: list[Path] = []
    for folder in folders:
        candidate = folder / LEGAL_FOLDER_NAME / name
        if candidate not in result:
            result.append(candidate)
    return result


def read_legal_text(base_name: str, language: str | None = None) -> str | None:
    """Text of LICENSE / THIRD_PARTY_NOTICES: a translation for the user's language if it exists, else English."""
    names = []
    if language and language != "en":
        names.append(f"{base_name}.{language}.txt")
    names.append(f"{base_name}.txt")
    for name in names:
        for candidate in legal_file_candidates(name):
            try:
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8-sig")
            except OSError:
                continue
    return None


class LicenseDialog(QDialog):
    """Read-only window with the license (or the third-party notices)."""

    def __init__(self, title: str, text: str, close_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(780, 620)
        layout = QVBoxLayout(self)
        self.viewer = QTextEdit()
        self.viewer.setReadOnly(True)
        self.viewer.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.viewer.setPlainText(text)
        layout.addWidget(self.viewer, 1)
        row = QHBoxLayout()
        row.addStretch()
        close = QPushButton(close_text)
        close.setProperty("primary", True)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)


class SettingsDialog(QDialog):
    def __init__(
        self,
        controller: "AppController",
        paths: AppPaths,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.paths = paths
        self._integrity_operation_id: str | None = None
        self._integrity_connected = False
        self._integrity_closed = False
        tr = controller.i18n.tr

        self.setWindowTitle(tr("settings.title"))
        self.resize(680, 650)

        outer_layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll, 1)

        info = QFrame()
        info.setProperty("role", "card")
        info_layout = QVBoxLayout(info)
        app_label = QLabel(f"{APP_NAME} {APP_VERSION}")
        app_label.setProperty("role", "subtitle")
        db_label = QLabel(
            tr("settings.database", path=paths.db_path)
        )
        db_label.setWordWrap(True)
        logs_label = QLabel(
            tr("settings.logs", path=paths.logs_dir)
        )
        logs_label.setWordWrap(True)
        info_layout.addWidget(app_label)
        info_layout.addWidget(db_label)
        info_layout.addWidget(logs_label)
        layout.addWidget(info)

        language_row = QHBoxLayout()
        language_row.addWidget(QLabel(tr("settings.language")))
        self.language_combo = QComboBox()
        for language_name, language_code in available_language_options():
            self.language_combo.addItem(language_name, language_code)
        current_index = self.language_combo.findData(
            controller.i18n.language
        )
        self.language_combo.setCurrentIndex(
            current_index if current_index >= 0 else 0
        )
        language_row.addWidget(self.language_combo)
        layout.addLayout(language_row)

        language_hint = QLabel(tr("settings.language_hint"))
        language_hint.setProperty("role", "muted")
        language_hint.setWordWrap(True)
        layout.addWidget(language_hint)

        runtime = QFrame()
        runtime.setProperty("role", "card")
        runtime_layout = QVBoxLayout(runtime)
        runtime_title = QLabel(tr("settings.runtime"))
        runtime_title.setProperty("role", "subtitle")
        runtime_layout.addWidget(runtime_title)

        self.close_to_tray_check = QCheckBox(
            tr("settings.close_to_tray")
        )
        self.close_to_tray_check.setChecked(
            self._get_bool_setting(
                SETTING_CLOSE_TO_TRAY, DEFAULT_CLOSE_TO_TRAY
            )
        )
        runtime_layout.addWidget(self.close_to_tray_check)

        self.start_minimized_check = QCheckBox(
            tr("settings.start_minimized")
        )
        self.start_minimized_check.setChecked(
            self._get_bool_setting(
                SETTING_START_MINIMIZED,
                DEFAULT_START_MINIMIZED,
            )
        )
        runtime_layout.addWidget(self.start_minimized_check)

        self.autostart_check = QCheckBox(
            tr("settings.autostart")
        )
        if controller.is_autostart_supported():
            try:
                self.autostart_check.setChecked(
                    controller.is_autostart_enabled()
                )
            except AppError:
                logging.exception(
                    "Unable to read Windows autostart status"
                )
                self.autostart_check.setChecked(False)
        else:
            self.autostart_check.setEnabled(False)
            self.autostart_check.setToolTip(
                tr("settings.autostart_unsupported")
            )
        runtime_layout.addWidget(self.autostart_check)

        self.runtime_status_label = QLabel()
        self.runtime_status_label.setProperty("role", "muted")
        self.runtime_status_label.setWordWrap(True)
        runtime_layout.addWidget(self.runtime_status_label)
        layout.addWidget(runtime)

        notifications = QFrame()
        notifications.setProperty("role", "card")
        notifications_layout = QVBoxLayout(notifications)
        title = QLabel(tr("settings.notifications"))
        title.setProperty("role", "subtitle")
        notifications_layout.addWidget(title)

        self.notifications_enabled_check = QCheckBox(
            tr("settings.important_notifications")
        )
        self.notifications_enabled_check.setChecked(
            self._get_bool_setting(
                SETTING_NOTIFICATIONS_ENABLED,
                DEFAULT_NOTIFICATIONS_ENABLED,
            )
        )
        notifications_layout.addWidget(
            self.notifications_enabled_check
        )

        self.success_notifications_check = QCheckBox(
            tr("settings.success_notifications")
        )
        self.success_notifications_check.setChecked(
            self._get_bool_setting(
                SETTING_NOTIFICATIONS_SUCCESS_ENABLED,
                DEFAULT_NOTIFICATIONS_SUCCESS_ENABLED,
            )
        )
        notifications_layout.addWidget(
            self.success_notifications_check
        )

        self.notification_cooldown_spin = QSpinBox()
        self.notification_cooldown_spin.setRange(
            MIN_NOTIFICATION_COOLDOWN_MINUTES,
            MAX_NOTIFICATION_COOLDOWN_MINUTES,
        )
        self.notification_cooldown_spin.setSuffix(
            tr("settings.minutes_suffix")
        )
        self.notification_cooldown_spin.setValue(
            self._get_cooldown_setting()
        )
        notification_form = QFormLayout()
        notification_form.addRow(
            tr("settings.cooldown"),
            self.notification_cooldown_spin,
        )
        notifications_layout.addLayout(notification_form)

        self.notification_status_label = QLabel()
        self.notification_status_label.setProperty(
            "role", "muted"
        )
        self.notification_status_label.setWordWrap(True)
        notifications_layout.addWidget(
            self.notification_status_label
        )
        layout.addWidget(notifications)

        diagnostics = QFrame()
        diagnostics.setProperty("role", "card")
        diagnostics_layout = QVBoxLayout(diagnostics)
        title = QLabel(tr("settings.diagnostics"))
        title.setProperty("role", "subtitle")
        diagnostics_layout.addWidget(title)

        integrity = QPushButton(tr("settings.check_database"))
        integrity.clicked.connect(self.check_integrity)
        logs_button = QPushButton(tr("settings.open_logs"))
        logs_button.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(paths.logs_dir))
            )
        )
        data_button = QPushButton(tr("settings.open_data"))
        data_button.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(paths.root))
            )
        )
        diagnostics_layout.addWidget(integrity)
        diagnostics_layout.addWidget(logs_button)
        diagnostics_layout.addWidget(data_button)
        layout.addWidget(diagnostics)

        about = QFrame()
        about.setProperty("role", "card")
        about_layout = QVBoxLayout(about)
        about_title = QLabel(tr("settings.about"))
        about_title.setProperty("role", "subtitle")
        author_label = QLabel(tr("settings.about_author", author=APP_AUTHOR))
        copyright_label = QLabel(APP_COPYRIGHT)
        copyright_label.setProperty("role", "muted")
        license_label = QLabel(tr("settings.about_license", license=APP_LICENSE_NAME))
        summary_label = QLabel(tr("license.summary"))
        summary_label.setProperty("role", "muted")
        summary_label.setWordWrap(True)
        self.license_button = QPushButton(tr("settings.license_button"))
        self.license_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.license_button.clicked.connect(
            lambda: self.show_legal_text("LICENSE", "license.title")
        )
        self.third_party_button = QPushButton(tr("settings.third_party_button"))
        self.third_party_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.third_party_button.clicked.connect(
            lambda: self.show_legal_text("THIRD_PARTY_NOTICES", "license.third_party_title")
        )
        about_layout.addWidget(about_title)
        about_layout.addWidget(author_label)
        about_layout.addWidget(copyright_label)
        about_layout.addWidget(license_label)
        about_layout.addWidget(summary_label)
        about_layout.addWidget(self.license_button)
        about_layout.addWidget(self.third_party_button)
        layout.addWidget(about)

        uninstall = QFrame()
        uninstall.setProperty("role", "card")
        uninstall_layout = QVBoxLayout(uninstall)
        uninstall_title = QLabel(tr("settings.uninstall"))
        uninstall_title.setProperty("role", "subtitle")
        uninstall_hint = QLabel(tr("settings.uninstall_hint"))
        uninstall_hint.setProperty("role", "muted")
        uninstall_hint.setWordWrap(True)
        self.uninstall_button = QPushButton(tr("settings.uninstall_button"))
        self.uninstall_button.setProperty("role", "danger")
        self.uninstall_button.setCursor(Qt.CursorShape.PointingHandCursor)
        if find_uninstaller() is None:
            self.uninstall_button.setEnabled(False)
            self.uninstall_button.setToolTip(
                tr("settings.uninstall_unavailable")
            )
        self.uninstall_button.clicked.connect(self.start_uninstall)
        uninstall_layout.addWidget(uninstall_title)
        uninstall_layout.addWidget(uninstall_hint)
        uninstall_layout.addWidget(self.uninstall_button)
        layout.addWidget(uninstall)

        self.close_to_tray_check.toggled.connect(
            self.update_runtime_controls
        )
        self.start_minimized_check.toggled.connect(
            self.update_runtime_controls
        )
        self.notifications_enabled_check.toggled.connect(
            self.update_notification_controls
        )
        self.success_notifications_check.toggled.connect(
            self.update_notification_controls
        )
        self.update_runtime_controls()
        self.update_notification_controls()

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton(tr("common.cancel"))
        cancel.clicked.connect(self.reject)
        save = QPushButton(tr("common.save"))
        save.setProperty("primary", True)
        save.clicked.connect(self.save)
        actions.addWidget(cancel)
        actions.addWidget(save)
        outer_layout.addLayout(actions)

    def _get_bool_setting(self, key: str, default: bool) -> bool:
        try:
            return self.controller.repo.get_bool_setting(
                key, default
            )
        except AppError:
            logging.exception(
                "Unable to read application setting: %s", key
            )
            return default

    def _get_cooldown_setting(self) -> int:
        try:
            value = self.controller.repo.get_setting(
                SETTING_NOTIFICATION_COOLDOWN_MINUTES,
                DEFAULT_NOTIFICATION_COOLDOWN_MINUTES,
            )
            if not isinstance(value, int):
                return DEFAULT_NOTIFICATION_COOLDOWN_MINUTES
            return max(
                MIN_NOTIFICATION_COOLDOWN_MINUTES,
                min(MAX_NOTIFICATION_COOLDOWN_MINUTES, value),
            )
        except AppError:
            logging.exception(
                "Unable to read notification cooldown setting"
            )
            return DEFAULT_NOTIFICATION_COOLDOWN_MINUTES

    def update_runtime_controls(self) -> None:
        close_to_tray = self.close_to_tray_check.isChecked()
        self.start_minimized_check.setEnabled(close_to_tray)
        if not close_to_tray:
            self.start_minimized_check.setChecked(False)

        tr = self.controller.i18n.tr
        if self.controller.active_operation_id:
            key = "settings.runtime_busy"
        elif close_to_tray:
            key = "settings.runtime_tray"
        else:
            key = "settings.runtime_exit"
        self.runtime_status_label.setText(tr(key))

    def update_notification_controls(self) -> None:
        enabled = self.notifications_enabled_check.isChecked()
        self.success_notifications_check.setEnabled(enabled)
        self.notification_cooldown_spin.setEnabled(enabled)
        if not enabled:
            self.success_notifications_check.setChecked(False)
        key = (
            "settings.notification_enabled_hint"
            if enabled else
            "settings.notification_disabled_hint"
        )
        self.notification_status_label.setText(
            self.controller.i18n.tr(key)
        )

    def show_legal_text(self, base_name: str, title_key: str) -> None:
        tr = self.controller.i18n.tr
        title = tr(title_key)
        text = read_legal_text(base_name, self.controller.i18n.language)
        if text is None:
            QMessageBox.information(self, title, tr("license.missing"))
            return
        dialog = LicenseDialog(title, text, tr("common.close"), self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def start_uninstall(self) -> None:
        tr = self.controller.i18n.tr
        title = tr("settings.uninstall_title")
        uninstaller = find_uninstaller()
        if uninstaller is None:
            QMessageBox.information(
                self, title, tr("settings.uninstall_unavailable")
            )
            return
        if self.controller.active_operation_id:
            QMessageBox.warning(self, title, tr("settings.uninstall_busy"))
            return
        answer = QMessageBox.question(
            self,
            title,
            tr("settings.uninstall_question"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            launch_uninstaller_helper(uninstaller)
        except OSError as exc:
            logging.exception("Unable to start the uninstaller helper")
            QMessageBox.warning(
                self, title, tr("settings.uninstall_failed", error=str(exc))
            )
            return
        logging.info("Uninstall requested from settings; closing the application")
        main_window = getattr(self.controller, "main_window", None)
        self.accept()
        if main_window is not None:
            QTimer.singleShot(0, main_window.request_explicit_exit)
        else:
            QTimer.singleShot(0, QApplication.quit)

    def save(self) -> None:
        tr = self.controller.i18n.tr
        try:
            close_to_tray = self.close_to_tray_check.isChecked()
            start_minimized = (
                self.start_minimized_check.isChecked()
                if close_to_tray else False
            )
            notifications_enabled = (
                self.notifications_enabled_check.isChecked()
            )
            success_enabled = (
                self.success_notifications_check.isChecked()
                if notifications_enabled else False
            )
            cooldown = self.notification_cooldown_spin.value()
            autostart_enabled: bool | None = None
            if self.autostart_check.isEnabled():
                autostart_enabled = (
                    self.autostart_check.isChecked()
                )

            selected_language = str(
                self.language_combo.currentData()
            )
            self.controller.i18n._validate_language(
                selected_language
            )
            old_language = self.controller.i18n.language

            self.controller.save_runtime_settings(
                close_to_tray=close_to_tray,
                start_minimized=start_minimized,
                autostart_enabled=autostart_enabled,
                notifications_enabled=notifications_enabled,
                success_notifications_enabled=success_enabled,
                notification_cooldown_minutes=cooldown,
            )
            self.controller.i18n.set_language(
                selected_language
            )

            self.controller.repo.activity(
                "info",
                "APP_RUNTIME_SETTINGS_UPDATED",
                "Application settings updated.",
                details={
                    "ui_language": selected_language,
                    "close_to_tray": close_to_tray,
                    "start_minimized": start_minimized,
                    "autostart_enabled": autostart_enabled,
                    "notifications_enabled": notifications_enabled,
                    "success_notifications_enabled": success_enabled,
                    "notification_cooldown_minutes": cooldown,
                },
            )

            parent = self.parent()
            self.accept()

            if isinstance(parent, MainWindow):
                if selected_language != old_language:
                    parent.apply_language()
                elif parent.tray_runtime is not None:
                    parent.tray_runtime.refresh()

        except AppError as exc:
            QMessageBox.warning(
                self,
                tr("settings.save_error_title"),
                self.controller.i18n.error_message(exc),
            )
        except Exception:
            logging.exception("Settings save failure")
            QMessageBox.critical(
                self,
                tr("settings.unexpected_error_title"),
                tr("settings.unexpected_error"),
            )

    def check_integrity(self) -> None:
        if self._integrity_operation_id is not None:
            return

        tr = self.controller.i18n.tr

        if not self._integrity_connected:
            self.controller.background_query_finished.connect(
                self._integrity_finished
            )
            self.finished.connect(
                self._integrity_dialog_closed
            )
            self._integrity_connected = True
            self._integrity_closed = False

        try:
            self._integrity_operation_id = (
                self.controller.queue_database_check()
            )
        except AppError as exc:
            QMessageBox.warning(
                self,
                tr("settings.integrity_error_title"),
                self.controller.i18n.error_message(exc),
            )
            return

        self._set_integrity_button_busy(True)

    def _set_integrity_button_busy(self, busy: bool) -> None:
        caption = self.controller.i18n.tr(
            "settings.check_database"
        )
        for button in self.findChildren(QPushButton):
            if button.text() == caption:
                button.setEnabled(not busy)

    def _integrity_finished(
        self,
        result: OperationResult,
    ) -> None:
        if self._integrity_closed:
            return

        if result.operation_id != self._integrity_operation_id:
            return

        self._integrity_operation_id = None
        self._set_integrity_button_busy(False)

        if self.controller._shutdown_requested:
            return

        tr = self.controller.i18n.tr

        if result.success:
            QMessageBox.information(
                self,
                tr("settings.integrity_title"),
                tr("settings.integrity_ok"),
            )

        elif result.error_code != "OPERATION_CANCELLED":
            QMessageBox.critical(
                self,
                tr("settings.integrity_error_title"),
                self.controller.i18n.operation_message(result)
                + "\n\n"
                + tr(
                    "dialog.error_code",
                    code=result.error_code or "UNKNOWN",
                ),
            )

    def _integrity_dialog_closed(self, result: int) -> None:
        self._integrity_closed = True
        self._integrity_operation_id = None

        if self._integrity_connected:
            try:
                self.controller.background_query_finished.disconnect(
                    self._integrity_finished
                )
            except (RuntimeError, TypeError):
                pass

            try:
                self.finished.disconnect(
                    self._integrity_dialog_closed
                )
            except (RuntimeError, TypeError):
                pass

            self._integrity_connected = False


class WindowsAutostartService:
    """Policy-driven tray notification adapter.

    The service does not create backup operations, does not change Issues and does not
    write a separate history. ActivityLog and Issues remain the history.
    """

    def __init__(
        self,
        executable_path: str | None = None,
        script_path: str | None = None,
        registry_module: Any = None,
        platform_name: str | None = None,
    ) -> None:
        self.executable_path = executable_path or sys.executable
        self.script_path = script_path or str(
            Path(sys.argv[0]).resolve()
        )
        self.registry_module = (
            registry_module
            if registry_module is not None
            else winreg
        )
        self.platform_name = platform_name or platform.system()

    def is_supported(self) -> bool:
        return (
            self.platform_name == "Windows"
            and self.registry_module is not None
        )

    @staticmethod
    def _quote(value: str) -> str:
        return subprocess.list2cmdline([value])

    def launch_command(self) -> str:
        executable = Path(self.executable_path)
        script = Path(self.script_path)

        if executable.suffix.lower() == ".exe" and (
            script.resolve() == executable.resolve()
            or script.name.lower() == executable.name.lower()
        ):
            return self._quote(str(executable))

        return (
            f"{self._quote(str(executable))} "
            f"{self._quote(str(script))}"
        )

    def is_enabled(self) -> bool:
        if not self.is_supported():
            return False

        try:
            with self.registry_module.OpenKey(
                self.registry_module.HKEY_CURRENT_USER,
                WINDOWS_RUN_KEY,
                0,
                self.registry_module.KEY_READ,
            ) as key:
                value, _value_type = self.registry_module.QueryValueEx(
                    key,
                    WINDOWS_AUTOSTART_VALUE_NAME,
                )

            return str(value) == self.launch_command()

        except FileNotFoundError:
            return False
        except OSError as exc:
            raise AppError(
                "AUTOSTART_READ_FAILED",
                "Could not read the Windows autostart setting.",
                str(exc),
            ) from exc

    def enable(self) -> None:
        if not self.is_supported():
            raise AppError(
                "AUTOSTART_UNSUPPORTED",
                "Autostart is available only on Windows.",
            )

        try:
            with self.registry_module.CreateKeyEx(
                self.registry_module.HKEY_CURRENT_USER,
                WINDOWS_RUN_KEY,
                0,
                self.registry_module.KEY_SET_VALUE,
            ) as key:
                self.registry_module.SetValueEx(
                    key,
                    WINDOWS_AUTOSTART_VALUE_NAME,
                    0,
                    self.registry_module.REG_SZ,
                    self.launch_command(),
                )

        except OSError as exc:
            raise AppError(
                "AUTOSTART_ENABLE_FAILED",
                "Could not enable Windows autostart.",
                str(exc),
            ) from exc

    def disable(self) -> None:
        if not self.is_supported():
            raise AppError(
                "AUTOSTART_UNSUPPORTED",
                "Autostart is available only on Windows.",
            )

        try:
            with self.registry_module.OpenKey(
                self.registry_module.HKEY_CURRENT_USER,
                WINDOWS_RUN_KEY,
                0,
                self.registry_module.KEY_SET_VALUE,
            ) as key:
                try:
                    self.registry_module.DeleteValue(
                        key,
                        WINDOWS_AUTOSTART_VALUE_NAME,
                    )
                except FileNotFoundError:
                    return

        except OSError as exc:
            raise AppError(
                "AUTOSTART_DISABLE_FAILED",
                "Could not disable Windows autostart.",
                str(exc),
            ) from exc


class NotificationService:
    """Versions available to the user, together with the plan data needed by UI actions."""

    CRITICAL_CODES = {
        "SOURCE_NOT_FOUND",
        "SOURCE_ACCESS_DENIED",
        "STORAGE_NOT_FOUND",
        "STORAGE_ACCESS_DENIED",
        "SNAPSHOT_CORRUPTED",
        "OPERATION_NEEDS_RECOVERY",
        "AUTO_BACKUP_FAILED",
        "RETENTION_BLOCKED_BY_PINS",
    }

    SUCCESS_CODES = {
        "AUTO_BACKUP_SUCCEEDED",
    }

    def __init__(
        self,
        repo: Repository,
        tray_provider: Callable[[], QSystemTrayIcon | None],
    ) -> None:
        self.repo = repo
        self.tray_provider = tray_provider

    def _notifications_enabled(self) -> bool:
        return self.repo.get_bool_setting(
            SETTING_NOTIFICATIONS_ENABLED,
            DEFAULT_NOTIFICATIONS_ENABLED,
        )

    def _success_enabled(self) -> bool:
        return self.repo.get_bool_setting(
            SETTING_NOTIFICATIONS_SUCCESS_ENABLED,
            DEFAULT_NOTIFICATIONS_SUCCESS_ENABLED,
        )

    def _cooldown_minutes(self) -> int:
        value = self.repo.get_setting(
            SETTING_NOTIFICATION_COOLDOWN_MINUTES,
            DEFAULT_NOTIFICATION_COOLDOWN_MINUTES,
        )

        if not isinstance(value, int):
            raise AppError(
                "NOTIFICATION_SETTING_INVALID",
                "The repeat notification interval must be an integer.",
            )

        if not (
            MIN_NOTIFICATION_COOLDOWN_MINUTES
            <= value
            <= MAX_NOTIFICATION_COOLDOWN_MINUTES
        ):
            raise AppError(
                "NOTIFICATION_SETTING_INVALID",
                "The repeat notification interval must be from "
                f"{MIN_NOTIFICATION_COOLDOWN_MINUTES} to "
                f"{MAX_NOTIFICATION_COOLDOWN_MINUTES} minutes.",
            )

        return value

    def _last_sent_by_key(self) -> dict[str, int]:
        value = self.repo.get_setting(
            SETTING_NOTIFICATION_LAST_SENT,
            {},
        )

        if not isinstance(value, dict):
            raise AppError(
                "NOTIFICATION_SETTING_INVALID",
                "The notification history has an invalid format.",
            )

        result: dict[str, int] = {}

        for key, sent_at in value.items():
            if isinstance(key, str) and isinstance(sent_at, int):
                result[key] = sent_at

        return result

    def _set_last_sent_by_key(
        self,
        value: dict[str, int],
    ) -> None:
        self.repo.set_setting(
            SETTING_NOTIFICATION_LAST_SENT,
            value,
        )

    @staticmethod
    def notification_key(
        code: str,
        plan_id: str | None,
    ) -> str:
        return f"{code}:{plan_id or 'global'}"

    @staticmethod
    def message_icon(
        severity: NotificationSeverity,
    ) -> QSystemTrayIcon.MessageIcon:
        if severity in {
            NotificationSeverity.ERROR,
            NotificationSeverity.CRITICAL,
        }:
            return QSystemTrayIcon.MessageIcon.Critical

        if severity == NotificationSeverity.WARNING:
            return QSystemTrayIcon.MessageIcon.Warning

        return QSystemTrayIcon.MessageIcon.Information

    def should_notify(
        self,
        code: str,
        plan_id: str | None,
        severity: NotificationSeverity,
        now_utc_ms: int,
        force: bool = False,
    ) -> bool:
        if force:
            return True

        if not self._notifications_enabled():
            return False

        if (
            code in self.SUCCESS_CODES
            and not self._success_enabled()
        ):
            return False

        key = self.notification_key(code, plan_id)
        history = self._last_sent_by_key()
        previous = history.get(key)

        if previous is None:
            return True

        cooldown_ms = self._cooldown_minutes() * 60 * 1000
        return now_utc_ms - previous >= cooldown_ms

    def notify(
        self,
        code: str,
        title: str,
        message: str,
        severity: NotificationSeverity,
        plan_id: str | None = None,
        now_utc_ms: int | None = None,
        force: bool = False,
    ) -> bool:
        now = utc_ms() if now_utc_ms is None else int(now_utc_ms)

        # ActivityLog.plan_id has a foreign key. Check the contract
        # before showing the notification and before changing the cooldown.
        if plan_id is not None:
            self.repo.plan_details(plan_id)

        if not self.should_notify(
            code,
            plan_id,
            severity,
            now,
            force,
        ):
            return False

        tray = self.tray_provider()

        if tray is None or not tray.isVisible():
            # A missing tray is a normal situation. The source event is already
            # stored in ActivityLog/Issues; we do not create an event on every tick.
            return False

        tray.showMessage(
            title,
            message,
            self.message_icon(severity),
            10_000,
        )

        history = self._last_sent_by_key()
        history[self.notification_key(code, plan_id)] = now
        self._set_last_sent_by_key(history)

        try:
            self.repo.activity(
                "info",
                "NOTIFICATION_SHOWN",
                f"Notification shown: {title}",
                plan_id,
                details={
                    "notification_code": code,
                    "severity": severity.value,
                },
            )
        except Exception:
            logging.exception(
                "Notification was shown, but its ActivityLog record failed"
            )

        return True


class TrayRuntime(QObject):
    def __init__(
        self,
        controller: "AppController",
        window: "MainWindow",
    ) -> None:
        super().__init__(window)
        self.controller = controller
        self.window = window
        self.tray: QSystemTrayIcon | None = None
        self.menu: QMenu | None = None
        self.status_action = None
        self.show_action = None
        self.health_action = None
        self.exit_action = None

    @staticmethod
    def is_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def start(self) -> bool:
        if not self.is_available():
            return False
        if self.tray is not None:
            return True

        tr = self.controller.i18n.tr
        icon = self.window.windowIcon()
        if icon.isNull():
            icon = self.window.style().standardIcon(
                self.window.style().StandardPixmap.SP_DriveHDIcon
            )

        self.tray = QSystemTrayIcon(icon, self.window)
        self.tray.setToolTip(APP_NAME)
        self.menu = QMenu(self.window)

        self.status_action = self.menu.addAction(
            tr("tray.status", status=tr("tray.ready"))
        )
        self.status_action.setEnabled(False)
        self.menu.addSeparator()

        self.show_action = self.menu.addAction(tr("tray.show"))
        self.show_action.triggered.connect(self.show_window)

        self.health_action = self.menu.addAction(tr("tray.health"))
        self.health_action.triggered.connect(
            self.window.show_health_center
        )
        self.menu.addSeparator()

        self.exit_action = self.menu.addAction(tr("tray.exit"))
        self.exit_action.triggered.connect(
            self.window.request_explicit_exit
        )

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_activated)
        self.tray.show()
        self.refresh()
        return True

    def stop(self) -> None:
        if self.tray is not None:
            self.tray.hide()
            self.tray.deleteLater()
            self.tray = None

        if self.menu is not None:
            self.menu.clear()
            self.menu.deleteLater()
            self.menu = None

        self.status_action = None
        self.show_action = None
        self.health_action = None
        self.exit_action = None

    def refresh(self) -> None:
        if self.tray is None or self.status_action is None:
            return

        tr = self.controller.i18n.tr
        if self.controller.active_operation_id:
            key = "tray.busy"
        else:
            try:
                counts = self.controller.repo.runtime_status_counts()
                if counts["active_errors"] > 0:
                    key = "tray.errors"
                elif counts["pending_plans"] > 0:
                    key = "tray.pending"
                elif counts["active_warnings"] > 0:
                    key = "tray.attention"
                elif counts["enabled_plans"] == 0:
                    key = "tray.no_plans"
                else:
                    key = "tray.protected"
            except Exception:
                logging.exception("Unable to refresh tray state")
                key = "tray.unavailable"

        status = tr(key)
        self.status_action.setText(
            tr("tray.status", status=status)
        )
        self.tray.setToolTip(f"{APP_NAME}: {status}")

    def retranslate(self) -> None:
        if self.tray is None:
            return

        tr = self.controller.i18n.tr
        if self.show_action is not None:
            self.show_action.setText(tr("tray.show"))
        if self.health_action is not None:
            self.health_action.setText(tr("tray.health"))
        if self.exit_action is not None:
            self.exit_action.setText(tr("tray.exit"))
        self.refresh()

    def show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def hide_window(self) -> None:
        self.window.hide()

    def on_activated(
        self,
        reason: QSystemTrayIcon.ActivationReason,
    ) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.show_window()


class ProtectionWizard19(QDialog):
    """Four-step protection setup. Saves a normal plan; never starts copying itself."""

    def __init__(self, controller: "AppController", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.created_plan_id: str | None = None
        self.start_first_backup = False
        self._source = None
        self._storage = None
        self.tr19 = controller.i18n.tr
        tr = self.tr19
        self.setWindowTitle(tr("setup.wizard.title"))
        self.resize(700, 480)
        root = QVBoxLayout(self)
        self.step_label = QLabel()
        self.step_label.setProperty("role", "subtitle")
        root.addWidget(self.step_label)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        page1 = QWidget(); l1 = QVBoxLayout(page1)
        l1.addWidget(self._help(tr("setup.wizard.s1_help")))
        self.folder_radio = QRadioButton(tr("setup.wizard.folder")); self.folder_radio.setChecked(True)
        self.file_radio = QRadioButton(tr("setup.wizard.file"))
        kind = QButtonGroup(self); kind.addButton(self.folder_radio); kind.addButton(self.file_radio)
        l1.addWidget(self.folder_radio); l1.addWidget(self.file_radio)
        row = QHBoxLayout()
        self.source_edit = QLineEdit(); row.addWidget(self.source_edit, 1)
        browse = QPushButton(tr("setup.wizard.browse")); browse.clicked.connect(self.pick_source); row.addWidget(browse)
        l1.addLayout(row)
        self.suggest_combo = QComboBox()
        self.suggest_combo.addItem(tr("setup.wizard.suggest"), "")
        for name in ("Documents", "Desktop", "Pictures"):
            candidate = Path.home() / name
            if candidate.is_dir():
                self.suggest_combo.addItem(str(candidate), str(candidate))
        self.suggest_combo.currentIndexChanged.connect(self.use_suggestion)
        l1.addWidget(self.suggest_combo); l1.addStretch()
        self.stack.addWidget(page1)

        page2 = QWidget(); l2 = QVBoxLayout(page2)
        l2.addWidget(self._help(tr("setup.wizard.s2_help")))
        row2 = QHBoxLayout()
        self.storage_edit = QLineEdit(); row2.addWidget(self.storage_edit, 1)
        browse2 = QPushButton(tr("setup.wizard.browse")); browse2.clicked.connect(self.pick_storage); row2.addWidget(browse2)
        l2.addLayout(row2); l2.addStretch()
        self.stack.addWidget(page2)

        page3 = QWidget(); l3 = QVBoxLayout(page3)
        l3.addWidget(self._help(tr("setup.wizard.s3_help")))
        self.manual_radio = QRadioButton(tr("setup.wizard.manual")); self.manual_radio.setChecked(True)
        self.daily_radio = QRadioButton(tr("setup.wizard.daily"))
        self.change_radio = QRadioButton(tr("setup.wizard.change"))
        how = QButtonGroup(self)
        for radio in (self.manual_radio, self.daily_radio, self.change_radio):
            how.addButton(radio); l3.addWidget(radio)
        self.expert_group = QGroupBox(tr("expert.expert"))
        self.expert_group.setCheckable(True); self.expert_group.setChecked(False)
        expert = QFormLayout(self.expert_group)
        self.expert_name = QLineEdit(); self.expert_name.setPlaceholderText(tr("expert.name_auto"))
        self.expert_mode = QComboBox()
        self.expert_mode.addItem(tr("expert.mode_direct"), StorageMode.DIRECT.value)
        self.expert_mode.addItem(tr("expert.mode_zip"), StorageMode.ZIP.value)
        self.expert_keep = QSpinBox(); self.expert_keep.setRange(MIN_KEEP_LAST_N, MAX_KEEP_LAST_N)
        self.expert_keep.setValue(DEFAULT_KEEP_LAST_N)
        self.expert_quiet = QCheckBox(tr("expert.quiet"))
        self.expert_quiet_from = QLineEdit("22:00"); self.expert_quiet_to = QLineEdit("07:00")
        quiet_row = QHBoxLayout()
        quiet_row.addWidget(self.expert_quiet); quiet_row.addWidget(self.expert_quiet_from)
        quiet_row.addWidget(QLabel("–")); quiet_row.addWidget(self.expert_quiet_to)
        expert.addRow(tr("expert.name"), self.expert_name)
        expert.addRow(tr("expert.mode"), self.expert_mode)
        expert.addRow(tr("expert.keep"), self.expert_keep)
        expert.addRow(quiet_row)
        hint = self._help(tr("expert.expert_help")); expert.addRow(hint)
        l3.addWidget(self.expert_group)
        l3.addStretch()
        self.stack.addWidget(page3)

        page4 = QWidget(); l4 = QVBoxLayout(page4)
        self.summary_label = QLabel(); self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        l4.addWidget(self.summary_label)
        self.first_backup_check = QCheckBox(tr("setup.wizard.first_backup"))
        self.first_backup_check.setChecked(False)
        l4.addWidget(self.first_backup_check); l4.addStretch()
        self.stack.addWidget(page4)

        buttons = QHBoxLayout(); buttons.addStretch()
        self.cancel_button = QPushButton(tr("setup.wizard.cancel")); self.cancel_button.clicked.connect(self.reject)
        self.back_button = QPushButton(tr("setup.wizard.back")); self.back_button.clicked.connect(self.go_back)
        self.next_button = QPushButton(tr("setup.wizard.next")); self.next_button.clicked.connect(self.go_next)
        for button in (self.cancel_button, self.back_button, self.next_button):
            buttons.addWidget(button)
        root.addLayout(buttons)
        self.show_step(0)
        self._path_drop = PathDropTarget(
            self, controller.i18n, self._drop_kind, self._apply_drop,
        )

    def _drop_kind(self) -> str | None:
        index = self.stack.currentIndex()
        if index == 0:
            return "source"
        if index == 1:
            return "folder"
        return None

    def _apply_drop(self, path: Path) -> None:
        if self.stack.currentIndex() == 0:
            self.source_edit.setText(str(path))
            if path.is_file():
                self.file_radio.setChecked(True)
            else:
                self.folder_radio.setChecked(True)
        elif self.stack.currentIndex() == 1:
            self.storage_edit.setText(str(path))

    @staticmethod
    def _help(text: str) -> QLabel:
        label = QLabel(text); label.setWordWrap(True); label.setProperty("role", "muted")
        return label

    def mode_text(self) -> str:
        tr = self.tr19
        if self.daily_radio.isChecked():
            return tr("setup.wizard.daily")
        if self.change_radio.isChecked():
            return tr("setup.wizard.change")
        return tr("setup.wizard.manual")

    def show_step(self, index: int) -> None:
        tr = self.tr19
        names = ("setup.wizard.s1", "setup.wizard.s2", "setup.wizard.s3", "setup.wizard.s4")
        self.stack.setCurrentIndex(index)
        self.step_label.setText(tr("setup.wizard.step", n=index + 1, name=tr(names[index])))
        self.back_button.setEnabled(index > 0)
        self.next_button.setText(tr("setup.wizard.finish") if index == 3 else tr("setup.wizard.next"))
        if index == 3:
            self.summary_label.setText(tr(
                "setup.wizard.summary",
                source=self.source_edit.text().strip() or tr("setup.wizard.none"),
                storage=self.storage_edit.text().strip() or tr("setup.wizard.none"),
                mode=self.mode_text()))
            if self.expert_group.isChecked():
                _name, storage_mode, keep = self.expert_values()
                extra = tr("expert.expert_summary", mode=tr("expert.mode_zip" if storage_mode == "zip" else "expert.mode_direct"),
                           keep=keep)
                if self._quiet_enabled():
                    extra += " " + tr("expert.quiet_summary", start=self.expert_quiet_from.text().strip(),
                                      end=self.expert_quiet_to.text().strip())
                self.summary_label.setText(self.summary_label.text() + "\n" + extra)

    def use_suggestion(self) -> None:
        value = self.suggest_combo.currentData()
        if value:
            self.source_edit.setText(str(value)); self.folder_radio.setChecked(True)

    def pick_source(self) -> None:
        if self.file_radio.isChecked():
            value, _filter = QFileDialog.getOpenFileName(self, self.tr19("setup.wizard.s1"))
        else:
            value = QFileDialog.getExistingDirectory(self, self.tr19("setup.wizard.s1"))
        if value:
            self.source_edit.setText(value)

    def pick_storage(self) -> None:
        value = QFileDialog.getExistingDirectory(self, self.tr19("setup.wizard.s2"))
        if value:
            self.storage_edit.setText(value)

    def go_back(self) -> None:
        self.show_step(max(0, self.stack.currentIndex() - 1))

    def _warn(self, exc: AppError) -> None:
        QMessageBox.warning(self, self.tr19("setup.wizard.title"), self.controller.i18n.error_message(exc))

    def go_next(self) -> None:
        index = self.stack.currentIndex()
        try:
            if index == 0:
                self._source = PathPolicy.validate_source(self.source_edit.text())
            elif index == 1:
                self._storage = PathPolicy.validate_storage(self.storage_edit.text())
                PathPolicy.validate_topology(self._source, self._storage)
            elif index == 2 and self._quiet_enabled():
                validate_local_time_text(self.expert_quiet_from.text().strip())
                validate_local_time_text(self.expert_quiet_to.text().strip())
            elif index == 3:
                self.finish_setup(); return
        except AppError as exc:
            self._warn(exc); return
        self.show_step(index + 1)

    def build_policy(self) -> TriggerPolicy:
        return TriggerPolicy(
            on_change_enabled=self.change_radio.isChecked(),
            change_debounce_seconds=DEFAULT_CHANGE_DEBOUNCE_SECONDS,
            min_auto_interval_minutes=DEFAULT_MIN_AUTO_INTERVAL_MINUTES,
            on_schedule_enabled=self.daily_radio.isChecked(),
            schedule_interval_minutes=1440 if self.daily_radio.isChecked() else DEFAULT_SCHEDULE_INTERVAL_MINUTES,
            quiet_hours_enabled=self._quiet_enabled(),
            quiet_hours_start_local=self.expert_quiet_from.text().strip() if self._quiet_enabled() else None,
            quiet_hours_end_local=self.expert_quiet_to.text().strip() if self._quiet_enabled() else None,
            manual_allowed_quiet_hours=True)

    def _quiet_enabled(self) -> bool:
        return self.expert_group.isChecked() and self.expert_quiet.isChecked()

    def expert_values(self) -> tuple[str, str, int]:
        if not self.expert_group.isChecked():
            return "", StorageMode.DIRECT.value, DEFAULT_KEEP_LAST_N
        return (self.expert_name.text().strip(), str(self.expert_mode.currentData()),
                self.expert_keep.value())

    def finish_setup(self) -> None:
        custom_name, mode, keep = self.expert_values()
        name = custom_name or Path(str(self._source)).name or str(self._source)
        try:
            self.created_plan_id = self.controller.save_plan_with_policy(
                None, name, "", self.source_edit.text(), self.storage_edit.text(),
                keep, mode, self.build_policy())
        except AppError as exc:
            self._warn(exc); return
        self.start_first_backup = self.first_backup_check.isChecked()
        self.accept()


class RestoreSelectedFilesDialog(QDialog):
    def __init__(
        self,
        controller: "AppController",
        snapshot_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.snapshot_id = snapshot_id
        self.selected_relative_paths: list[str] = []
        tr = controller.i18n.tr

        self.setWindowTitle(tr("restore_selected.title"))
        self.resize(760, 560)

        layout = QVBoxLayout(self)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            tr("restore_selected.search")
        )
        self.search_edit.textChanged.connect(self.refresh_files)
        layout.addWidget(self.search_edit)

        self.files_list = QTableWidget(0, 2)
        self.files_list.setHorizontalHeaderLabels(
            [
                "",
                tr("restore_selected.files"),
            ]
        )
        self.files_list.verticalHeader().setVisible(False)
        self.files_list.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.files_list.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.files_list.horizontalHeader().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.Stretch,
        )
        layout.addWidget(self.files_list, 1)

        self.selected_label = QLabel(
            tr("restore_selected.selected", count=0)
        )
        self.selected_label.setProperty("role", "muted")
        layout.addWidget(self.selected_label)

        destination_row = QHBoxLayout()
        destination_row.addWidget(
            QLabel(tr("restore_selected.destination"))
        )

        self.destination_edit = QLineEdit()
        destination_row.addWidget(self.destination_edit, 1)

        choose = QPushButton(
            tr("restore_selected.choose_destination")
        )
        choose.clicked.connect(self.choose_destination)
        destination_row.addWidget(choose)
        layout.addLayout(destination_row)

        actions = QHBoxLayout()
        actions.addStretch()

        cancel = QPushButton(tr("common.cancel"))
        cancel.clicked.connect(self.reject)

        restore = QPushButton(tr("snapshot.restore_selected"))
        restore.setProperty("primary", True)
        restore.clicked.connect(self.accept_selection)

        actions.addWidget(cancel)
        actions.addWidget(restore)
        layout.addLayout(actions)

        self._file_paths = self._load_snapshot_files()
        self.refresh_files()
        self._path_drop = PathDropTarget(
            self, controller.i18n, "folder",
            lambda path: self.destination_edit.setText(str(path)),
            title=controller.i18n.tr("drop.folder_title"),
            detail=controller.i18n.tr("drop.folder_detail"),
        )

    def _load_snapshot_files(self) -> list[str]:
        paths = self.controller.cached_snapshot_files(
            self.snapshot_id
        )
        if paths is None:
            raise AppError(
                "SNAPSHOT_FILES_NOT_READY",
                "The file list has not been loaded yet.",
            )
        return paths

    def refresh_files(self) -> None:
        search = self.search_edit.text().strip().casefold()
        selected = set(self.selected_relative_paths)

        visible = [
            value
            for value in self._file_paths
            if not search or search in value.casefold()
        ]

        self.files_list.setRowCount(len(visible))

        for row, relative_path in enumerate(visible):
            checkbox = QCheckBox()
            checkbox.setChecked(relative_path in selected)
            checkbox.setProperty("relative_path", relative_path)
            checkbox.stateChanged.connect(
                self.update_selection_from_table
            )

            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(
                Qt.AlignmentFlag.AlignCenter
            )
            holder_layout.addWidget(checkbox)

            self.files_list.setCellWidget(row, 0, holder)
            self.files_list.setItem(
                row,
                1,
                QTableWidgetItem(relative_path),
            )

        self.update_selected_label()

    def update_selection_from_table(self) -> None:
        selected = set(self.selected_relative_paths)

        for row in range(self.files_list.rowCount()):
            holder = self.files_list.cellWidget(row, 0)
            if holder is None:
                continue

            checkbox = holder.findChild(QCheckBox)
            if checkbox is None:
                continue

            relative_path = str(
                checkbox.property("relative_path")
            )

            if checkbox.isChecked():
                selected.add(relative_path)
            else:
                selected.discard(relative_path)

        self.selected_relative_paths = sorted(
            selected,
            key=str.casefold,
        )
        self.update_selected_label()

    def update_selected_label(self) -> None:
        self.selected_label.setText(
            self.controller.i18n.tr(
                "restore_selected.selected",
                count=len(self.selected_relative_paths),
            )
        )

    def choose_destination(self) -> None:
        start = self.destination_edit.text().strip()
        if start and not Path(start).is_dir():
            start = str(Path(start).parent)
        selected = QFileDialog.getExistingDirectory(
            self,
            self.controller.i18n.tr(
                "dialog.choose_restore_selected_directory"
            ),
            start,
        )
        if selected:
            self.destination_edit.setText(selected)

    def accept_selection(self) -> None:
        tr = self.controller.i18n.tr

        if not self.selected_relative_paths:
            QMessageBox.warning(
                self,
                tr("restore_selected.title"),
                tr("restore_selected.none"),
            )
            return

        if not self.destination_edit.text().strip():
            QMessageBox.warning(
                self,
                tr("restore_selected.title"),
                tr("restore_selected.destination_missing"),
            )
            return

        self.accept()



SETTING_HELP_SHOWN = "ui.help_shown"
HELP_PAGES = (("about", 2), ("add", 3), ("plan", 4), ("restore", 2), ("tray", 3), ("more", 3))
HELP_MOCK_W, HELP_MOCK_H = 700, 270


class _MockPainter:
    """Draws simplified pseudo-screenshots with labels taken from the translation dictionary."""

    BG, PANEL, LINE = "#0e121a", "#151c27", "#303b50"
    TEXT, MUTED, ACCENT, GREEN, MARK = "#edf2f8", "#9aa8bc", "#6d7cff", "#4fca8a", "#ffb020"

    def __init__(self, tr: Callable[..., str]) -> None:
        from PySide6.QtGui import QPixmap, QPainter
        self.tr = tr
        self.pixmap = QPixmap(HELP_MOCK_W * 2, HELP_MOCK_H * 2)
        self.pixmap.setDevicePixelRatio(2)
        self.pixmap.fill(QColor(self.BG))
        self.p = QPainter(self.pixmap)
        self.p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    def finish(self):
        self.p.end()
        return self.pixmap

    def rect(self, x, y, w, h, fill=None, border=None, radius=6, width=1):
        from PySide6.QtGui import QPen, QBrush
        self.p.setPen(QPen(QColor(border), width) if border else Qt.PenStyle.NoPen)
        self.p.setBrush(QBrush(QColor(fill)) if fill else Qt.BrushStyle.NoBrush)
        self.p.drawRoundedRect(x, y, w, h, radius, radius)

    def text(self, x, y, w, h, value, color=None, size=10, bold=False, align=None):
        from PySide6.QtGui import QFont, QPen
        font = QFont("Segoe UI")
        font.setPointSizeF(size)
        font.setBold(bold)
        self.p.setFont(font)
        self.p.setPen(QPen(QColor(color or self.TEXT)))
        flags = (align or Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        self.p.drawText(QRect(int(x), int(y), int(w), int(h)), int(flags), value)

    def width_of(self, value, size=10, bold=False) -> int:
        from PySide6.QtGui import QFont, QFontMetrics
        font = QFont("Segoe UI")
        font.setPointSizeF(size)
        font.setBold(bold)
        return QFontMetrics(font).horizontalAdvance(value)

    def button(self, x, y, h, value, primary=False, selected=False):
        w = self.width_of(value, 10, primary) + 28
        if primary:
            self.rect(x, y, w, h, fill=self.ACCENT, radius=7)
        elif selected:
            self.rect(x, y, w, h, fill="#2f4a8a", radius=6)
        else:
            self.rect(x, y, w, h, fill="#2a3446", border="#3a475e", radius=6)
        self.text(x, y, w, h, value, "#ffffff" if (primary or selected) else self.TEXT, 10,
                  primary or selected, Qt.AlignmentFlag.AlignHCenter)
        return QRect(x, y, w, h)

    def mark(self, area: QRect, number: int, pad: int = 3):
        from PySide6.QtGui import QPen, QBrush, QFont
        box = area.adjusted(-pad, -pad, pad, pad)
        self.p.setPen(QPen(QColor(self.MARK), 3))
        self.p.setBrush(Qt.BrushStyle.NoBrush)
        self.p.drawRoundedRect(box, 8, 8)
        cx, cy = box.left() - 2, box.top() - 2
        self.p.setPen(Qt.PenStyle.NoPen)
        self.p.setBrush(QBrush(QColor(self.MARK)))
        self.p.drawEllipse(QPoint(cx, cy), 11, 11)
        font = QFont("Segoe UI")
        font.setPointSizeF(10)
        font.setBold(True)
        self.p.setFont(font)
        self.p.setPen(QPen(QColor("#1a1206")))
        self.p.drawText(QRect(cx - 11, cy - 11, 22, 22), int(Qt.AlignmentFlag.AlignCenter), str(number))

    def main_window(self, nav_selected="overview", empty=False, with_card=False, list_rows=False):
        tr = self.tr
        self.rect(0, 0, HELP_MOCK_W, HELP_MOCK_H, fill="#10151e", border=self.LINE, radius=8)
        self.rect(1, 1, HELP_MOCK_W - 2, 50, fill=self.PANEL, radius=8)
        self.text(16, 8, 200, 34, APP_NAME, self.TEXT, 13, True)
        add_rect = self.button(HELP_MOCK_W - 190, 9, 34, tr("home.add"), primary=True)
        self.rect(1, 51, HELP_MOCK_W - 2, 38, fill="#151c27", radius=0)
        x = 12
        rects = {}
        for nav_id, key in (("overview", "nav.overview"), ("data", "nav.data"),
                            ("restore", "nav.restore"), ("events", "nav.events"),
                            ("settings", "nav.settings"), ("help", "help.nav.help")):
            r = self.button(x, 55, 30, tr(key), selected=(nav_id == nav_selected))
            rects[nav_id] = r
            x += r.width() + 6
        self.p.setPen(Qt.PenStyle.NoPen)
        health_rect = QRect(1, 90, HELP_MOCK_W - 2, 34)
        self.rect(1, 90, HELP_MOCK_W - 2, 34, fill=self.PANEL, radius=0)
        hx = 20
        for count, key, color in (("1", "health.state.protected", self.GREEN), ("0", "health.state.attention", "#f3bd4f"),
                                  ("0", "health.state.error", "#ef6974")):
            self.text(hx, 90, 20, 34, count, color, 11, True)
            label = tr(key)
            self.text(hx + 22, 90, 140, 34, label, self.MUTED, 10)
            hx += 22 + self.width_of(label) + 34
        card_rect = QRect()
        if with_card:
            card_rect = QRect(16, 138, HELP_MOCK_W - 32, 100)
            self.rect(card_rect.x(), card_rect.y(), card_rect.width(), card_rect.height(), fill=self.PANEL, border=self.LINE, radius=10)
            self.p.setBrush(QColor(self.GREEN)); self.p.setPen(Qt.PenStyle.NoPen)
            self.p.drawEllipse(QPoint(40, 170), 6, 6)
            self.text(56, 156, 400, 28, tr("help.mock.plan_name"), self.TEXT, 12, True)
            self.text(56, 186, 500, 24, tr("help.mock.plan_detail"), self.MUTED, 10)
            self.text(56, 208, 500, 24, tr("health.state.protected"), self.GREEN, 10, True)
        if empty:
            self.text(0, 150, HELP_MOCK_W, 40, tr("help.mock.empty"), self.MUTED, 12, False, Qt.AlignmentFlag.AlignHCenter)
        if list_rows:
            for index, key in enumerate(("help.mock.copy1", "help.mock.copy2", "help.mock.copy3")):
                top = 138 + index * 40
                self.rect(16, top, HELP_MOCK_W - 32, 34, fill=self.PANEL, border=self.LINE, radius=8)
                self.text(30, top, 400, 34, tr(key), self.TEXT, 10)
            card_rect = QRect(16, 138, HELP_MOCK_W - 32, 34)
        return {"add": add_rect, "nav": rects, "health": health_rect, "card": card_rect}


def build_help_mock(kind: str, tr: Callable[..., str]):
    m = _MockPainter(tr)
    try:
        _draw_help_mock(m, kind, tr)
    except BaseException:
        m.p.end()
        raise
    return m.finish()


def _draw_help_mock(m: "_MockPainter", kind: str, tr: Callable[..., str]) -> None:
    p = m.p
    if kind == "about":
        r = m.main_window(with_card=True)
        m.mark(r["health"].adjusted(4, 2, -4, -2), 1)
        m.mark(r["card"], 2)
    elif kind == "add":
        r = m.main_window(empty=True)
        m.mark(r["add"], 1)
    elif kind == "plan":
        m.rect(20, 6, HELP_MOCK_W - 40, HELP_MOCK_H - 12, fill=m.PANEL, border=m.LINE, radius=10)
        m.text(36, 12, 400, 30, tr("help.mock.dialog_title"), m.TEXT, 12, True)
        sw_basic = m.button(36, 46, 30, tr("planform.mode.basic"), selected=True)
        m.button(36 + sw_basic.width() + 4, 46, 30, tr("planform.mode.advanced"))
        switch = QRect(36, 46, sw_basic.width() + 4 + m.width_of(tr("planform.mode.advanced")) + 28, 30)
        m.text(36, 88, 120, 28, tr("help.mock.source"), m.MUTED, 10)
        m.rect(160, 88, 400, 28, fill=m.BG, border="#3a475e", radius=6)
        m.text(170, 88, 380, 28, "C:\\Users\\Anna\\Documents", m.TEXT, 10)
        m.button(570, 88, 28, tr("editor.browse"))
        source = QRect(160, 88, 400, 28)
        m.text(36, 124, 120, 28, tr("help.mock.storage"), m.MUTED, 10)
        m.rect(160, 124, 400, 28, fill=m.BG, border="#3a475e", radius=6)
        m.text(170, 124, 380, 28, "D:\\Backups", m.TEXT, 10)
        m.button(570, 124, 28, tr("editor.browse"))
        history = QRect(160, 158, 400, 28)
        m.rect(160, 158, 400, 28, fill=m.BG, border="#3a475e", radius=6)
        m.text(170, 158, 360, 28, tr("folders.history.placeholder"), m.MUTED, 10)
        m.text(530, 158, 24, 28, "v", m.MUTED, 10, False, Qt.AlignmentFlag.AlignHCenter)
        m.text(36, 196, 560, 24, tr("help.mock.frequency"), m.MUTED, 10)
        create = m.button(HELP_MOCK_W - 40 - 28 - m.width_of(tr("help.mock.create"), 10, True) - 28 + 28, 222, 34, tr("help.mock.create"), primary=True)
        m.mark(switch, 1)
        m.mark(source, 2)
        m.mark(history, 3)
        m.mark(create, 4)
    elif kind == "restore":
        r = m.main_window(nav_selected="restore", list_rows=True)
        m.mark(r["nav"]["restore"], 1)
        m.mark(r["card"], 2)
    elif kind == "tray":
        m.rect(0, 0, HELP_MOCK_W, HELP_MOCK_H, fill="#161c28", border=m.LINE, radius=8)
        m.rect(0, HELP_MOCK_H - 46, HELP_MOCK_W, 46, fill="#1d2536", radius=0)
        m.text(HELP_MOCK_W - 70, HELP_MOCK_H - 46, 60, 46, "14:05", m.TEXT, 10, False, Qt.AlignmentFlag.AlignHCenter)
        m.text(HELP_MOCK_W - 190, HELP_MOCK_H - 46, 24, 46, "^", m.MUTED, 11, True, Qt.AlignmentFlag.AlignHCenter)
        for dx in (150, 122):
            m.rect(HELP_MOCK_W - dx, HELP_MOCK_H - 33, 20, 20, fill="#3a475e", radius=10)
        icon = QRect(HELP_MOCK_W - 162 + 2, HELP_MOCK_H - 35, 24, 24)
        m.rect(icon.x(), icon.y(), 24, 24, fill=m.ACCENT, radius=7)
        m.text(icon.x(), icon.y(), 24, 24, "A", "#ffffff", 10, True, Qt.AlignmentFlag.AlignHCenter)
        menu = QRect(HELP_MOCK_W - 330, HELP_MOCK_H - 46 - 96, 190, 88)
        m.rect(menu.x(), menu.y(), menu.width(), menu.height(), fill="#1a2130", border="#3a475e", radius=8)
        m.text(menu.x() + 14, menu.y() + 8, 170, 34, tr("help.mock.tray_open"), m.TEXT, 10)
        m.text(menu.x() + 14, menu.y() + 46, 170, 34, tr("help.mock.tray_exit"), m.TEXT, 10)
        m.mark(icon, 1)
        m.mark(QRect(menu.x() + 6, menu.y() + 6, menu.width() - 12, 36), 2)
        m.mark(QRect(menu.x() + 6, menu.y() + 44, menu.width() - 12, 36), 3)
    else:
        r = m.main_window(nav_selected="settings")
        m.mark(r["nav"]["settings"], 1)
        m.mark(r["nav"]["help"], 2)
        m.mark(r["add"], 3)


class HelpDialog(QDialog):
    def __init__(self, controller: "AppController", parent: QWidget | None = None, page: int = 0) -> None:
        super().__init__(parent)
        self.controller = controller
        tr = controller.i18n.tr
        self.tr_ = tr
        self.setWindowTitle(tr("help.title"))
        self.setMinimumSize(1000, 660)
        outer = QVBoxLayout(self)
        self.nav = QListWidget()
        self.nav.setFixedWidth(210)
        self.stack = QStackedWidget()
        for kind, _count in HELP_PAGES:
            self.nav.addItem(tr(f"help.page.{kind}.nav"))
            self.stack.addWidget(self._build_page(kind))
        body = QHBoxLayout()
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)
        buttons = QHBoxLayout()
        self.back_button = QPushButton(tr("help.back"))
        self.next_button = QPushButton(tr("help.next"))
        close = QPushButton(tr("common.close"))
        close.setProperty("primary", True)
        self.back_button.clicked.connect(lambda: self.nav.setCurrentRow(max(0, self.nav.currentRow() - 1)))
        self.next_button.clicked.connect(lambda: self.nav.setCurrentRow(min(self.nav.count() - 1, self.nav.currentRow() + 1)))
        close.clicked.connect(self.accept)
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        buttons.addStretch()
        buttons.addWidget(close)
        outer.addLayout(buttons)
        self.nav.currentRowChanged.connect(self._on_page)
        self.nav.setCurrentRow(max(0, min(page, self.nav.count() - 1)))
        self._on_page(self.nav.currentRow())

    def _on_page(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        self.back_button.setEnabled(row > 0)
        self.next_button.setEnabled(row < self.nav.count() - 1)

    def _build_page(self, kind: str) -> QWidget:
        tr = self.tr_
        count = dict(HELP_PAGES)[kind]
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        lay = QVBoxLayout(holder)
        title = QLabel(tr(f"help.page.{kind}.title"))
        title.setStyleSheet("font-size:20px;font-weight:700;")
        title.setWordWrap(True)
        lay.addWidget(title)
        body = QLabel(tr(f"help.page.{kind}.body"))
        body.setWordWrap(True)
        body.setProperty("role", "muted")
        lay.addWidget(body)
        image = QLabel()
        image.setPixmap(build_help_mock(kind, tr))
        image.setFixedSize(HELP_MOCK_W, HELP_MOCK_H)
        image.setAccessibleDescription(tr(f"help.page.{kind}.title"))
        lay.addWidget(image, 0, Qt.AlignmentFlag.AlignLeft)
        for number in range(1, count + 1):
            row = QHBoxLayout()
            badge = QLabel(str(number))
            badge.setFixedSize(24, 24)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet("background:#ffb020;color:#1a1206;border-radius:12px;font-weight:700;")
            text = QLabel(tr(f"help.page.{kind}.c{number}"))
            text.setWordWrap(True)
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(text, 1)
            lay.addLayout(row)
        lay.addStretch()
        scroll.setWidget(holder)
        return scroll


class MainWindow(QMainWindow):
    def __init__(
        self,
        controller: "AppController",
        paths: AppPaths,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.paths = paths

        self.current_plan_id: str | None = None

        self._pending_snapshot_files: tuple[
            str, str
        ] | None = None
        self._pending_restore_destination19 = ""
        self._pending_restore_confirm19 = False

        self._health_refresh_operation_id: str | None = None

        self.plan_cards: dict[str, PlanCard] = {}
        self.tray_runtime: TrayRuntime | None = None
        self.progress_frame: QFrame | None = None

        self._explicit_exit_requested = False
        self._shutdown_close_scheduled = False
        self._shutdown_close_timer: QTimer | None = None

        self._ui_refresh_pending = False
        self._ui_refresh_health_requested = False
        self._ui_refresh_timer = QTimer(self)
        self._ui_refresh_timer.setSingleShot(True)
        self._ui_refresh_timer.setInterval(0)
        self._ui_refresh_timer.timeout.connect(self._flush_ui_refresh)
        self._versions_sig19 = None
        self._versions_timer19 = QTimer(self)
        self._versions_timer19.setInterval(1500)
        self._versions_timer19.timeout.connect(self.sync_versions19)
        self._versions_timer19.start()
        for sequence, handler in (("Ctrl+N", lambda: self.guided_setup19()),
                                  ("Ctrl+B", lambda: self.quick_backup19()),
                                  ("F5", lambda: self.refresh())):
            QShortcut(QKeySequence(sequence), self, activated=handler)
        self._search_refresh_timer = QTimer(self)
        self._search_refresh_timer.setSingleShot(True)
        self._search_refresh_timer.setInterval(150)
        self._search_refresh_timer.timeout.connect(self._flush_search_refresh)

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setMinimumSize(780, 540)
        self.resize(1280, 820)
        self._build_ui()
        self._path_drop = PathDropTarget(
            self, controller.i18n, "source", self._open_protection_for_path,
            allow_multiple=True, on_many=self._drop_many_sources,
        )

        self.controller.operation_progress.connect(
            self.on_progress
        )
        self.controller.operation_finished.connect(
            self.on_finished
        )
        self.controller.shutdown_finished.connect(self._cancel_ui_refresh)
        self.controller.shutdown_finished.connect(
            self._shutdown_ready
        )
        self.controller.background_query_finished.connect(
            self._snapshot_files_loaded
        )
        self.controller.health_refresh_finished.connect(
            self._health_refresh_loaded
        )
        self.refresh()
        self._auto_restore_timer19 = QTimer(self)
        self._auto_restore_timer19.setInterval(15 * 60 * 1000)
        self._auto_restore_timer19.timeout.connect(self._auto_restore_tick19)
        self._auto_restore_timer19.start()
        self._auto_restore_first19 = QTimer(self)
        self._auto_restore_first19.setSingleShot(True)
        self._auto_restore_first19.timeout.connect(self._auto_restore_tick19)
        self._auto_restore_first19.start(90_000)

    def setup_tray_runtime(self) -> bool:
        if self.tray_runtime is None:
            self.tray_runtime = TrayRuntime(
                self.controller,
                self,
            )

        return self.tray_runtime.start()


    def close_to_tray_enabled(self) -> bool:
        try:
            return self.controller.repo.get_bool_setting(
                SETTING_CLOSE_TO_TRAY,
                DEFAULT_CLOSE_TO_TRAY,
            )
        except AppError:
            logging.exception("Unable to load close-to-tray setting")
            return False


    def bring_to_front(self) -> None:
        """A second launch asked this copy to show itself (from the tray, minimized or hidden)."""
        if getattr(self.controller, "_shutdown_requested", False):
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _schedule_application_quit(self) -> None:
        QTimer.singleShot(0, QApplication.quit)

    def request_explicit_exit(self) -> None:
        self._explicit_exit_requested = True
        if self.controller.active_operation_id and not self.isVisible():
            self.showNormal()
        self.close()

    def make_button(self, text: str, callback: Callable[[], None], primary: bool = False, role: str | None = None) -> QPushButton:
        button = QPushButton(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        if primary:
            button.setProperty("primary", True)
        if role:
            button.setProperty("role", role)
        return button

    def _build_ui(self) -> None:
        if self.progress_frame is not None:
            self.progress_frame.hide()
            self.progress_frame.setParent(None)
            self.progress_frame.deleteLater()
            self.progress_frame = None

        tr = self.controller.i18n.tr
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top = QFrame()
        top.setStyleSheet(
            "background:#18202e; border-bottom:1px solid #354157;"
        )
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(14, 8, 14, 8)

        product = QLabel(APP_NAME)
        product.setProperty("role", "title")
        version = QLabel(APP_VERSION)
        version.setProperty("role", "muted")
        top_layout.addWidget(product)
        top_layout.addWidget(version)
        top_layout.addStretch()
        more_button = QPushButton(tr("overview.more"))
        more_button.setProperty("role", "ghost")
        more_menu = QMenu(more_button)
        more_menu.addAction(tr("overview.advanced_plan"), self.new_plan_advanced)
        more_menu.addAction(tr("app.health_center"), self.show_health_center)
        more_button.setMenu(more_menu)
        top_layout.addWidget(more_button)
        add_top = self.make_button(tr("home.add"), self.guided_setup19, primary=True)
        add_top.setMinimumHeight(38)
        add_top.setStyleSheet("font-size:14px;font-weight:600;padding:6px 18px;")
        add_top.setToolTip(tr("home.add_tip"))
        top_layout.addWidget(add_top)
        outer.addWidget(top)

        nav = QFrame()
        nav.setStyleSheet("background:#151c27; border-bottom:1px solid #303b50;")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(10, 4, 10, 4)
        self.nav_buttons19: dict[str, QPushButton] = {}
        nav_style = ("QPushButton{font-size:14px;padding:8px 16px;border:none;border-radius:6px;"
                     "background:transparent;color:#cdd6e6;}QPushButton:hover{background:#243044;}"
                     "QPushButton:checked{background:#2f4a8a;color:white;font-weight:600;}")
        for nav_id, key, callback in (("overview", "nav.overview", self.show_overview19),
                                      ("data", "nav.data", self.show_my_data19),
                                      ("restore", "nav.restore", self.show_recovery_center19),
                                      ("events", "nav.events", self.show_events19),
                                      ("settings", "nav.settings", self.show_settings),
                                      ("support", "support.nav", self.show_support),
                                      ("help", "help.nav.help", self.show_help)):
            button = self.make_button(tr(key), callback)
            button.setCheckable(nav_id in {"overview", "data"})
            button.setStyleSheet(nav_style)
            button.setToolTip(tr(key + "_tip"))
            self.nav_buttons19[nav_id] = button
            nav_layout.addWidget(button)
        self.nav_buttons19["overview"].setChecked(True)
        nav_layout.addStretch()
        outer.addWidget(nav)
        self.recovery_overview19 = QLabel()
        self.health_bar = QFrame()
        self.health_bar.setStyleSheet(
            "background:#151c27; border-bottom:1px solid #303b50;"
        )
        health_layout = QHBoxLayout(self.health_bar)
        health_layout.setContentsMargins(14, 7, 14, 7)
        self.health_counts: dict[str, QLabel] = {}

        for state, color in (
            (HealthState.PROTECTED.value, "#4fca8a"),
            (HealthState.ATTENTION.value, "#f3bd4f"),
            (HealthState.ERROR.value, "#ef6974"),
            (HealthState.RUNNING.value, "#7c9cff"),
            (HealthState.UNPROTECTED.value, "#9aa8bc"),
        ):
            holder = QWidget()
            holder.setCursor(Qt.CursorShape.PointingHandCursor)
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(6, 2, 6, 2)

            count = QLabel("0")
            count.setStyleSheet(f"font-weight:700; color:{color};")
            caption = QLabel(tr(f"health.state.{state}"))
            caption.setProperty("role", "muted")

            holder_layout.addWidget(count)
            holder_layout.addWidget(caption)
            health_layout.addWidget(holder)
            self.health_counts[state] = count

            holder.mousePressEvent = (
                lambda _event, health_state=state:
                self.open_health_filter(health_state)
            )

        health_layout.addStretch()
        self.health_focus19 = HealthState.ERROR.value
        self.health_fix19 = QPushButton(tr("healthcenter.hb_fix"))
        self.health_fix19.setProperty("role", "ghost")
        self.health_fix19.setCursor(Qt.CursorShape.PointingHandCursor)
        self.health_fix19.clicked.connect(lambda: self.open_health_filter(self.health_focus19))
        self.health_fix19.hide()
        health_layout.addWidget(self.health_fix19)
        outer.addWidget(self.health_bar)

        splitter = QSplitter()
        outer.addWidget(splitter, 1)

        sidebar = QWidget()
        sidebar.setMinimumWidth(220)
        sidebar.setMaximumWidth(360)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 12, 12, 12)
        sidebar_layout.addWidget(QLabel(tr("main.plans")))

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(tr("main.search"))
        self.search_edit.textChanged.connect(self._schedule_search_refresh)
        sidebar_layout.addWidget(self.search_edit)

        self.plans_scroll = QScrollArea()
        self.plans_scroll.setWidgetResizable(True)
        self.plans_container = QWidget()
        self.plans_layout = QVBoxLayout(self.plans_container)
        self.plans_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.plans_layout.setSpacing(8)
        self.plans_scroll.setWidget(self.plans_container)
        sidebar_layout.addWidget(self.plans_scroll, 1)
        splitter.addWidget(sidebar)

        self.stack = QStackedWidget()
        splitter.addWidget(self.stack)
        splitter.setSizes([300, 940])
        splitter.setChildrenCollapsible(False)

        self.empty_view = QWidget()
        empty_layout = QVBoxLayout(self.empty_view)
        empty_layout.setContentsMargins(24, 24, 24, 24)
        empty_layout.addWidget(
            EmptyState(
                tr("main.first_plan"),
                tr("main.first_plan_detail"),
                tr("home.add"),
                self.guided_setup19,
            )
        )
        steps = QLabel(tr("home.steps")); steps.setWordWrap(True)
        steps.setAlignment(Qt.AlignmentFlag.AlignCenter); steps.setProperty("role", "muted")
        empty_layout.addWidget(steps)
        self.stack.addWidget(self.empty_view)

        self.overview_view = QWidget()
        overview_layout = QVBoxLayout(self.overview_view)
        overview_layout.setContentsMargins(24, 24, 24, 24)
        self.overview_headline19 = QLabel()
        self.overview_headline19.setStyleSheet("font-size:26px;font-weight:700;")
        self.overview_headline19.setWordWrap(True)
        overview_layout.addWidget(self.overview_headline19)
        self.overview_action19 = self.make_button(
            tr("overview.do"), self.run_recommended19, primary=True)
        self.overview_action19.setMinimumHeight(40)
        self.overview_action19.setStyleSheet("font-size:14px;font-weight:600;padding:8px 22px;")
        overview_layout.addWidget(self.overview_action19, alignment=Qt.AlignmentFlag.AlignLeft)
        tiles = QHBoxLayout(); tiles.setSpacing(14)
        self.tile_add19 = self.make_tile19("tile.add", "home.add_tip", self.guided_setup19, True)
        self.tile_backup19 = self.make_tile19("tile.backup", "home.tile_backup_tip", self.quick_backup19, False)
        self.tile_restore19 = self.make_tile19("tile.restore", "home.tile_restore_tip", self.show_recovery_center19, False)
        for tile in (self.tile_add19, self.tile_backup19, self.tile_restore19):
            tiles.addWidget(tile, 1)
        overview_layout.addLayout(tiles)
        sets_title = QLabel(tr("datasets.title")); sets_title.setProperty("role", "title")
        overview_layout.addWidget(sets_title)
        sets_holder = QWidget()
        self.overview_sets19 = QVBoxLayout(sets_holder)
        self.overview_sets19.setContentsMargins(0, 0, 0, 0)
        self.overview_sets19.setSpacing(8)
        self.overview_sets19.setAlignment(Qt.AlignmentFlag.AlignTop)
        sets_scroll = QScrollArea()
        sets_scroll.setWidgetResizable(True)
        sets_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sets_scroll.setWidget(sets_holder)
        overview_layout.addWidget(sets_scroll, 1)
        details_title = QLabel(tr("home.status_details")); details_title.setProperty("role", "muted")
        overview_layout.addWidget(details_title)
        self.recovery_overview19.setParent(self.overview_view)
        self.recovery_overview19.setWordWrap(True)
        self.recovery_overview19.setProperty("role", "muted")
        self.recovery_overview19.setTextFormat(Qt.TextFormat.PlainText)
        overview_layout.addWidget(self.recovery_overview19)
        self.stack.addWidget(self.overview_view)

        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.detail_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        self.detail_view = QWidget()
        self.detail_view.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        self.detail_layout = QVBoxLayout(self.detail_view)
        self.detail_layout.setContentsMargins(24, 18, 24, 18)
        self.detail_layout.setSpacing(14)
        self.detail_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.detail_scroll.setWidget(self.detail_view)
        self.stack.addWidget(self.detail_scroll)

        self.progress_frame = QFrame(self)
        self.progress_frame.setStyleSheet("background:#10141bee;")
        progress_layout = QVBoxLayout(self.progress_frame)
        progress_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.progress_title = QLabel("")
        self.progress_title.setProperty("role", "title")
        self.progress_path = QLabel("")
        self.progress_path.setProperty("role", "muted")
        self.progress_path.setWordWrap(True)
        self.progress_path.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumWidth(200)
        self.cancel_button = self.make_button(
            tr("common.cancel"),
            self.cancel_active,
            role="ghost",
        )

        progress_layout.addWidget(
            self.progress_title,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        progress_layout.addWidget(
            self.progress_path,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        progress_layout.addWidget(
            self.progress_bar,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        progress_layout.addWidget(
            self.cancel_button,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        self.progress_frame.hide()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "progress_frame"):
            self._place_progress19()

    def _health_refresh_loaded(
        self,
        result: OperationResult,
    ) -> None:
        self._health_refresh_operation_id = None

        if (
            self.controller._shutdown_requested
            or not result.success
        ):
            return

        # Show only the accepted cache.
        # No new request is created here: otherwise every result
        # would trigger the next health refresh.
        self.request_refresh(request_health_refresh=False)

    def _ui_refresh_allowed(self) -> bool:
        return not (
            self.controller._shutdown_requested
            or self.controller._shutdown_completed
        )

    def _cancel_ui_refresh(self) -> None:
        self._ui_refresh_timer.stop()
        self._search_refresh_timer.stop()
        self._ui_refresh_pending = False
        self._ui_refresh_health_requested = False

    def request_refresh(self, request_health_refresh: bool = True) -> None:
        if not self._ui_refresh_allowed():
            self._cancel_ui_refresh()
            return
        self._ui_refresh_pending = True
        self._ui_refresh_health_requested |= request_health_refresh
        if not self._ui_refresh_timer.isActive():
            self._ui_refresh_timer.start()

    def _flush_ui_refresh(self) -> None:
        pending = self._ui_refresh_pending
        request_health = self._ui_refresh_health_requested
        self._cancel_ui_refresh()
        if pending and self._ui_refresh_allowed():
            self.refresh(request_health_refresh=request_health)

    def _schedule_search_refresh(self, _text: str) -> None:
        if self._ui_refresh_allowed():
            self._search_refresh_timer.start()
        else:
            self._cancel_ui_refresh()

    def _flush_search_refresh(self) -> None:
        if not self._ui_refresh_allowed():
            self._cancel_ui_refresh()
            return
        # A pending full refresh already includes the latest search text.
        if not self._ui_refresh_pending:
            self.refresh_plans()

    def refresh(
        self,
        request_health_refresh: bool = True,
    ) -> None:
        self._cancel_ui_refresh()
        if not self._ui_refresh_allowed():
            return
        self.refresh_plans()
        self.refresh_health_bar()

        if self.current_plan_id:
            self.show_plan_detail(self.current_plan_id)
        elif self.stack.currentWidget() in (self.empty_view, self.overview_view):
            self.stack.setCurrentWidget(
                self.overview_view
                if self.controller.repo.plan_summaries(False)
                else self.empty_view
            )

        if (
            request_health_refresh
            and self._health_refresh_operation_id is None
        ):
            operation_id = self.controller.queue_health_refresh()

            if operation_id is not None:
                self._health_refresh_operation_id = operation_id

    def apply_language(self) -> None:
        apply_layout_direction(self.controller.i18n.language)
        search_text = self.search_edit.text()
        selected_plan_id = self.current_plan_id

        overlay = self.progress_frame
        saved_progress = (
            overlay is not None and not overlay.isHidden(),
            self.progress_title.text(),
            self.progress_path.text(),
            self.progress_bar.minimum(),
            self.progress_bar.maximum(),
            self.progress_bar.value(),
            not self.cancel_button.isHidden(),
        )

        old_central = self.takeCentralWidget()
        self.plan_cards.clear()
        self.current_plan_id = selected_plan_id

        self._build_ui()

        self.search_edit.blockSignals(True)
        self.search_edit.setText(search_text)
        self.search_edit.blockSignals(False)
        self.refresh()

        (
            visible,
            title,
            path,
            minimum,
            maximum,
            value,
            cancellable,
        ) = saved_progress

        self.progress_title.setText(title)
        self.progress_path.setText(path)
        self.progress_bar.setRange(minimum, maximum)
        self.progress_bar.setValue(value)
        self.cancel_button.setVisible(cancellable)

        if visible:
            self._place_progress19()
            self.progress_frame.show()
            self.progress_frame.raise_()

        if old_central is not None:
            old_central.deleteLater()

        if self.tray_runtime is not None:
            self.tray_runtime.retranslate()

    def refresh_health_bar(self) -> None:
        self.recovery_overview19.setTextFormat(Qt.TextFormat.PlainText)
        overview_state = self.overview_state19()
        self.recovery_overview19.setText(self.overview_text19(overview_state))
        self.overview_action19.setVisible(overview_state["action"] != "ok")
        self.overview_action19.setEnabled(not self.controller.active_operation_id)
        self.tile_backup19.setEnabled(not self.controller.active_operation_id)
        self.overview_headline19.setText(self.overview_headline_text19(overview_state))
        self.refresh_overview_sets19()
        self.overview_action19.setText(
            self.controller.i18n.tr("home.do." + overview_state["action"]))
        count = {
            key: 0
            for key in self.health_counts
        }

        summaries = (
            self.controller.repo.plan_summaries(False)
            + self.controller.repo.plan_summaries(True)
        )

        for summary in summaries:
            health = self.controller.cached_plan_health(
                summary.id
            )

            if health.state in count:
                count[health.state] += 1

        for key, label in self.health_counts.items():
            label.setText(str(count[key]))
        self.update_health_summary19(overview_state, count)

    def update_health_summary19(self, overview_state: dict, count: dict) -> None:
        issues = self.controller.repo.list_active_issues()
        errors = max(
            sum(1 for item in issues if item.severity in {IssueSeverity.ERROR.value, IssueSeverity.CRITICAL.value}),
            count.get(HealthState.ERROR.value, 0))
        warnings = max(
            sum(1 for item in issues if item.severity == IssueSeverity.WARNING.value),
            count.get(HealthState.ATTENTION.value, 0))
        self.health_focus19 = HealthState.ERROR.value if errors else HealthState.ATTENTION.value
        self.health_fix19.setVisible(bool(errors or warnings))

    def refresh_plans(self) -> None:
        clear_layout_recursively(self.plans_layout)
        self.plan_cards.clear()

        search = self.search_edit.text().strip().casefold()

        summaries = (
            self.controller.repo.plan_summaries(False)
            + self.controller.repo.plan_summaries(True)
        )
        self.search_edit.setVisible(len(summaries) > 5 or bool(search))

        summaries = [
            item
            for item in summaries
            if (
                not search
                or search in item.display_name.casefold()
                or search in item.source_path.casefold()
            )
        ]

        if not summaries:
            self.plans_layout.addWidget(
                EmptyState(
                    self.controller.i18n.tr("main.no_plans"),
                    self.controller.i18n.tr(
                        "main.no_plans_detail"
                    ),
                )
            )
            return

        for summary in summaries:
            health = self.controller.cached_plan_health(
                summary.id
            )

            card = PlanCard(
                summary,
                PlanHealth(
                    plan_id=health.plan_id,
                    state=health.state,
                    title=self.controller.i18n.tr(
                        f"health.state.{health.state}"
                    ),
                    detail=health.detail,
                    source_available=health.source_available,
                    storage_available=health.storage_available,
                    committed_snapshot_count=(
                        health.committed_snapshot_count
                    ),
                    last_snapshot_at_utc_ms=(
                        health.last_snapshot_at_utc_ms
                    ),
                    last_operation_state=(
                        health.last_operation_state
                    ),
                    active_issue_count=health.active_issue_count,
                ),
                summary.id == self.current_plan_id,
            )

            card.clicked.connect(self.select_plan_by_id)
            self.plan_cards[summary.id] = card
            self.plans_layout.addWidget(card)

        self.plans_layout.addStretch()

    def select_plan_by_id(self, plan_id: str) -> None:
        self.current_plan_id = plan_id
        self.show_plan_detail(plan_id)
        self.refresh_plans()

    def clear_detail(self) -> None:
        clear_layout_recursively(self.detail_layout)
        self.versions_table = None
        self.bulk_panel = None

    def automation_summary_text(self, plan: PlanDetails) -> str:
        tr = self.controller.i18n.tr
        modes: list[str] = []

        if plan.on_change_enabled:
            modes.append(tr("plan.automation.change"))
        if plan.on_schedule_enabled:
            modes.append(
                tr(
                    "plan.automation.schedule",
                    minutes=plan.schedule_interval_minutes,
                )
            )
        if not modes:
            return tr("plan.automation.off")

        text = tr("plan.automation.on", modes="; ".join(modes))
        if plan.quiet_hours_enabled:
            text += ". " + tr(
                "plan.automation.quiet",
                start=plan.quiet_hours_start_local,
                end=plan.quiet_hours_end_local,
            )
        return text

    def pending_automation_text(self, plan: PlanDetails) -> str:
        tr = self.controller.i18n.tr
        if plan.pending_trigger_mask == 0:
            return tr("plan.pending.none")

        reasons: list[str] = []
        if has_trigger_mask(
            plan.pending_trigger_mask, TRIGGER_MASK_EVENT
        ):
            reasons.append(tr("plan.pending.event"))
        if has_trigger_mask(
            plan.pending_trigger_mask, TRIGGER_MASK_SCHEDULE
        ):
            reasons.append(tr("plan.pending.schedule"))
        if has_trigger_mask(
            plan.pending_trigger_mask, TRIGGER_MASK_STARTUP_CATCHUP
        ):
            reasons.append(tr("plan.pending.startup"))

        return tr(
            "plan.pending.text",
            reasons=", ".join(reasons),
            time=fmt_time(plan.pending_trigger_since_utc_ms),
        )

    def confirm_source_identity(self, plan_id: str) -> None:
        tr = self.controller.i18n.tr
        answer = QMessageBox.question(
            self, tr("source.confirm_title"), tr("source.confirm_question"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.confirm_source_identity(plan_id)
            self.refresh()
        except AppError as exc:
            QMessageBox.warning(self, tr("dialog.operation_failed"), self.controller.i18n.error_message(exc))

    def show_plan_detail(self, plan_id: str) -> None:
        self.clear_detail()
        try:
            plan = self.controller.repo.plan_details(plan_id)
        except AppError:
            self.stack.setCurrentWidget(self.empty_view)
            return

        tr = self.controller.i18n.tr

        health = self.controller.cached_plan_health(plan_id)

        self.stack.setCurrentWidget(self.detail_scroll)
        self.set_nav19("data")

        header = QHBoxLayout()
        name = QLabel(plan.display_name)
        name.setProperty("role", "title")
        header.addWidget(name)
        header.addStretch()
        header.addWidget(
            StatusBadge(
                health.state,
                tr(f"health.state.{health.state}"),
            )
        )
        if self.controller.source_confirmation_required(plan_id):
            header.addWidget(self.make_button(
                tr("source.confirm"), lambda: self.confirm_source_identity(plan_id), role="danger",
            ))
        header.addWidget(
            self.make_button(
                tr("plan.edit"),
                lambda: self.edit_plan(plan_id),
                role="ghost",
            )
        )
        self.detail_layout.addLayout(header)

        health_detail = QLabel(
            self.controller.i18n.health_detail(health.state)
        )
        health_detail.setProperty("role", "muted")
        health_detail.setWordWrap(True)
        self.detail_layout.addWidget(health_detail)

        info = QFrame()
        info.setProperty("role", "surface")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(12, 10, 12, 10)

        mode = tr(
            "plan.mode.direct"
            if plan.storage_mode == StorageMode.DIRECT.value
            else "plan.mode.zip"
        )
        info_lines = (
            tr("plan.source", path=plan.source_path),
            tr("plan.storage", path=plan.storage_path),
            tr("plan.mode", mode=mode),
            (
                tr("plan.keep", count=plan.keep_last_n)
                if plan.retention_mode == RETENTION_MODE_COUNT
                else tr("plan.retention_time", daily=plan.retention_daily,
                        weekly=plan.retention_weekly, monthly=plan.retention_monthly)
            ),
            self.automation_summary_text(plan),
            self.pending_automation_text(plan),
            tr(
                "plan.last_auto",
                time=fmt_time(plan.last_auto_backup_at_utc_ms),
            ),
        )
        for text in (info_lines[0], info_lines[1], info_lines[4], info_lines[5], info_lines[6]):
            label = QLabel(text)
            label.setWordWrap(True)
            info_layout.addWidget(label)
        technical = QLabel(info_lines[2] + "\n" + info_lines[3])
        technical.setWordWrap(True); technical.setProperty("role", "muted"); technical.hide()
        technical_toggle = QPushButton(tr("home.tech_show")); technical_toggle.setProperty("role", "ghost")
        technical_toggle.setCheckable(True)
        technical_toggle.toggled.connect(lambda on, label=technical, button=technical_toggle: (
            label.setVisible(on), button.setText(tr("home.tech_hide" if on else "home.tech_show"))))
        info_layout.addWidget(technical_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        info_layout.addWidget(technical)
        self.detail_layout.addWidget(info)

        committed = self.controller.repo.available_snapshots(plan_id)
        storage_available = health.storage_available
        storage_free_bytes = self.controller.cached_storage_free_bytes(
            plan_id
        )
        metrics = QHBoxLayout()
        metrics.addWidget(
            MetricCard(
                tr("metric.last_version"),
                fmt_time(health.last_snapshot_at_utc_ms),
                tr("metric.total", count=len(committed)),
            ),
            1,
        )
        metrics.addWidget(
            MetricCard(
                tr("metric.size"),
                fmt_size(
                    sum(item.stored_size_bytes for item in committed)
                ),
            ),
            1,
        )
        metrics.addWidget(
            MetricCard(
                tr("metric.free"),
                    (
                        fmt_size(storage_free_bytes or 0)
                        if storage_available
                        else tr("metric.unavailable")
                    )
            ),
            1,
        )
        self.detail_layout.addLayout(metrics)

        actions = FlowLayout(spacing=8)
        action_font = "font-size:14px;font-weight:600;padding:8px 20px;"
        backup_button = self.make_button(tr("home.backup_now"), lambda: self.queue_backup(plan_id), primary=True)
        backup_button.setMinimumHeight(40)
        backup_button.setStyleSheet(action_font)
        backup_button.setToolTip(tr("home.tile_backup_tip"))
        restore_button = self.make_button(
            tr("home.restore"),
            lambda: (setattr(self, "_recovery_preselect19", plan_id), self.show_recovery_center19()))
        restore_button.setMinimumHeight(40)
        restore_button.setStyleSheet(action_font)
        restore_button.setToolTip(tr("home.tile_restore_tip"))
        actions.addWidget(backup_button)
        actions.addWidget(restore_button)
        deletion_pending = self.controller.repo.plan_deletion_job(plan_id) is not None
        if self.controller.repo.needs_recovery_for_plan(plan_id) and not deletion_pending:
            recover_button = self.make_button(
                tr("health.recover"), lambda: self.start_recovery(plan_id), role="danger")
            recover_button.setMinimumHeight(40)
            actions.addWidget(recover_button)
        for label_key, callback, role in (
            ("action.force_version", lambda: self.queue_backup(plan_id, True), None),
            ("action.verify_latest", lambda: self.verify_latest(plan_id), None),
            ("action.archive", lambda: self.archive_plan(plan_id), None),
            ("plan_delete.action",
             lambda: self.delete_plan_data(plan_id), "danger"),
        ):
            button = self.make_button(tr(label_key), callback, role=role) if role else self.make_button(tr(label_key), callback)
            button.setMinimumHeight(40)
            actions.addWidget(button)
        actions_holder = QWidget()
        actions_holder.setLayout(actions)
        self.detail_layout.addWidget(actions_holder)

        title = QLabel(tr("versions.title"))
        title.setProperty("role", "title")
        self.detail_layout.addWidget(title)

        self.versions_table = QTableWidget(0, 9)
        self.versions_table.setHorizontalHeaderLabels(
            [
                "",
                tr("versions.column.number"),
                tr("versions.column.created"),
                tr("versions.column.mode"),
                tr("versions.column.size"),
                tr("versions.column.files"),
                tr("versions.column.state"),
                tr("versions.column.verification"),
                tr("versions.column.actions"),
            ]
        )
        self.versions_table.setColumnHidden(3, True)
        header_view = self.versions_table.horizontalHeader()
        for column in range(8):
            header_view.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        header_view.setSectionResizeMode(
            8, QHeaderView.ResizeMode.ResizeToContents
        )
        self.versions_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.versions_table.setWordWrap(False)
        header_view.setStretchLastSection(True)
        self.versions_table.verticalHeader().setDefaultSectionSize(48)
        self.versions_table.verticalHeader().setVisible(False)
        self.versions_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.versions_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.versions_table.setAlternatingRowColors(True)
        self._versions_sig19 = self.versions_signature19(plan_id)
        self.populate_versions(plan_id, committed)
        self.versions_table.setMinimumHeight(min(560, max(250, 40 + 48 * len(committed))))
        self.versions_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.detail_layout.addWidget(self.versions_table)

        self.bulk_panel = QWidget()
        bulk_layout = QHBoxLayout(self.bulk_panel)
        self.bulk_caption = QLabel(
            tr("bulk.selected", count=0)
        )
        self.bulk_caption.setProperty("role", "muted")
        bulk_layout.addWidget(self.bulk_caption)
        bulk_layout.addStretch()
        bulk_layout.addWidget(
            self.make_button(
                tr("bulk.delete"),
                self.delete_selected,
                role="danger",
            )
        )
        bulk_layout.addWidget(
            self.make_button(
                tr("bulk.clear"),
                self.clear_selection,
                role="ghost",
            )
        )
        self.bulk_panel.hide()
        self.detail_layout.addWidget(self.bulk_panel)

    def versions_signature19(self, plan_id: str) -> tuple:
        return tuple(
            (item.id, item.state, item.verified_at_utc_ms, bool(item.pinned))
            for item in self.controller.repo.available_snapshots(plan_id)
        )

    def sync_versions19(self) -> None:
        plan_id = self.current_plan_id
        if (not plan_id or not self._ui_refresh_allowed()
                or self.stack.currentWidget() is not self.detail_scroll
                or QApplication.activePopupWidget() is not None
                or not getattr(self, "bulk_panel", QWidget()).isHidden()):
            return
        try:
            signature = self.versions_signature19(plan_id)
        except AppError:
            return
        if signature != getattr(self, "_versions_sig19", None):
            self.show_plan_detail(plan_id)

    def populate_versions(
        self,
        plan_id: str,
        snapshots: list[SnapshotInfo],
    ) -> None:
        self.versions_table.setRowCount(len(snapshots))
        tr = self.controller.i18n.tr

        mode_names = {
            StorageMode.DIRECT.value: tr("versions.mode.direct"),
            StorageMode.ZIP.value: "ZIP",
        }
        verification_colors = {
            "not_verified": "#9aa8bc",
            "passed": "#4fca8a",
            "failed": "#ef6974",
        }

        for row, snapshot in enumerate(snapshots):
            checkbox = QCheckBox()
            checkbox.setProperty("snapshot_id", snapshot.id)
            checkbox.setEnabled(
                snapshot.state == "committed" and not snapshot.pinned
            )
            checkbox.stateChanged.connect(self.update_bulk_panel)

            checkbox_holder = QWidget()
            checkbox_layout = QHBoxLayout(checkbox_holder)
            checkbox_layout.setContentsMargins(0, 0, 0, 0)
            checkbox_layout.setAlignment(
                Qt.AlignmentFlag.AlignCenter
            )
            checkbox_layout.addWidget(checkbox)
            self.versions_table.setCellWidget(row, 0, checkbox_holder)

            for column, value in (
                (1, str(snapshot.sequence_no)),
                (2, fmt_time(snapshot.created_at_utc_ms)),
                (
                    3,
                    mode_names.get(
                        snapshot.storage_mode,
                        snapshot.storage_mode,
                    ),
                ),
                (4, fmt_size(snapshot.stored_size_bytes)),
                (5, str(snapshot.file_count)),
            ):
                self.versions_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )

            state_key = f"versions.state.{snapshot.state}"
            state_text = (
                tr(state_key)
                if state_key in TRANSLATIONS[Language.ENGLISH.value]
                else snapshot.state
            )

            if snapshot.pinned:
                state_text += f" · {tr('versions.state.pinned')}"

            state_item = QTableWidgetItem(state_text)

            if snapshot.state == "committed":
                state_item.setForeground(QColor("#4fca8a"))
            elif snapshot.state in {
                "corrupted",
                "needs_recovery",
            }:
                state_item.setForeground(QColor("#ef6974"))

            self.versions_table.setItem(row, 6, state_item)

            verification_key = (
                f"versions.verify.{snapshot.verification_state}"
            )
            verification_text = (
                tr(verification_key)
                if verification_key
                in TRANSLATIONS[Language.ENGLISH.value]
                else snapshot.verification_state
            )

            verification_item = QTableWidgetItem(
                verification_text
            )
            verification_item.setForeground(
                QColor(
                    verification_colors.get(
                        snapshot.verification_state,
                        "#9aa8bc",
                    )
                )
            )
            self.versions_table.setItem(
                row,
                7,
                verification_item,
            )

            holder = QWidget()
            actions = QHBoxLayout(holder)
            actions.setContentsMargins(4, 4, 4, 4)
            actions.setSpacing(6)

            if snapshot.state == "committed":
                sid = snapshot.id
                restore = QPushButton(tr("snapshot.restore"))
                restore.setToolTip(tr("snapshot.restore_tooltip"))
                restore.setProperty("primary", True)
                restore.setCursor(Qt.CursorShape.PointingHandCursor)
                restore.setMinimumSize(110, 32)
                restore.clicked.connect(
                    lambda _checked=False, snapshot_id=sid: self.queue_restore(snapshot_id))
                more = QPushButton(tr("versions.row_more"))
                more.setProperty("role", "ghost")
                more.setCursor(Qt.CursorShape.PointingHandCursor)
                more.setMinimumSize(110, 32)
                menu = QMenu(more)
                restore_to_action = menu.addAction(
                    tr("snapshot.restore_to"),
                    lambda _c=False, snapshot_id=sid: self.restore_to_directory(snapshot_id))
                restore_to_action.setToolTip(tr("snapshot.restore_to_tooltip"))
                menu.setToolTipsVisible(True)
                if snapshot.source_kind == SourceKind.FOLDER.value:
                    menu.addAction(tr("snapshot.restore_selected"),
                                   lambda _c=False, snapshot_id=sid: self.restore_selected_files(snapshot_id))
                if snapshot.source_kind == SourceKind.FILE.value:
                    menu.addAction(tr("snapshot.replace_file"),
                                   lambda _c=False, snapshot_id=sid: self.replace_original_file(snapshot_id))
                menu.addSeparator()
                menu.addAction(tr("recovery.test_button"), lambda _c=False, snapshot_id=sid: self.queue_test_restore19(snapshot_id))
                menu.addAction(tr("snapshot.verify"), lambda _c=False, snapshot_id=sid: self.queue_verify(snapshot_id))
                menu.addAction(tr("snapshot.unpin" if snapshot.pinned else "snapshot.pin"),
                               lambda _c=False, snapshot_id=sid, pinned=snapshot.pinned:
                               self.set_pin(snapshot_id, not pinned))
                more.setMenu(menu)
                actions.addWidget(restore)
                actions.addWidget(more)

            actions.addStretch()
            self.versions_table.setCellWidget(row, 8, holder)
            self.versions_table.setRowHeight(row, 48)

    def selected_snapshot_ids(self) -> list[str]:
        selected: list[str] = []
        for row in range(self.versions_table.rowCount()):
            holder = self.versions_table.cellWidget(row, 0)
            if holder is None:
                continue
            checkbox = holder.findChild(QCheckBox)
            if checkbox is not None and checkbox.isChecked():
                selected.append(str(checkbox.property("snapshot_id")))
        return selected

    def update_bulk_panel(self) -> None:
        count = len(self.selected_snapshot_ids())
        self.bulk_caption.setText(
            self.controller.i18n.tr(
                "bulk.selected",
                count=count,
            )
        )
        self.bulk_panel.setVisible(count > 0)

    def clear_selection(self) -> None:
        for row in range(self.versions_table.rowCount()):
            holder = self.versions_table.cellWidget(row, 0)
            if holder:
                checkbox = holder.findChild(QCheckBox)
                if checkbox:
                    checkbox.blockSignals(True)
                    checkbox.setChecked(False)
                    checkbox.blockSignals(False)
        self.update_bulk_panel()

    def delete_selected(self) -> None:
        ids = self.selected_snapshot_ids()
        if not ids:
            return
        tr = self.controller.i18n.tr
        answer = QMessageBox.question(
            self,
            tr("dialog.delete_title"),
            tr("dialog.delete_question", count=len(ids)),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.queue_delete(ids)

    def show_progress(self, title: str, path: str = "", cancellable: bool = True) -> None:
        self.progress_title.setText(title)
        self.progress_path.setText(path)
        self.cancel_button.setVisible(cancellable)
        self.progress_bar.setRange(0, 0)
        self._place_progress19()
        self.progress_frame.show()
        self.progress_frame.raise_()
        self.overview_action19.setEnabled(False)
        self.tile_backup19.setEnabled(False)

    def on_progress(self, event: ProgressEvent) -> None:
        if (
            event.operation_id
            != self.controller.active_operation_id
        ):
            return

        language = self.controller.i18n.language
        if self.controller._shutdown_requested:
            self.progress_path.setText(localized_progress_path(event.current_path, language))
            self.cancel_button.hide()
            return

        self.progress_title.setText(localized_progress_title(event.title, language))
        self.progress_path.setText(localized_progress_path(event.current_path, language))
        self.cancel_button.setVisible(event.cancellable)

        if event.total_files > 0:
            total = event.total_files
            done = event.completed_files
        elif event.total_bytes > 0:
            total = event.total_bytes
            done = event.completed_bytes
        else:
            self.progress_bar.setRange(0, 0)
            return

        done = max(0, min(done, total))

        if total <= 2_147_483_647:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
        else:
            self.progress_bar.setRange(0, 10000)
            self.progress_bar.setValue(
                (done * 10000) // total
            )

    def on_finished(self, result: OperationResult) -> None:
        if self.controller._shutdown_requested:
            return

        self.progress_frame.hide()
        self.sync_versions19()
        is_automatic = result.trigger_reason in {
            TriggerReason.EVENT.value,
            TriggerReason.SCHEDULE.value,
            TriggerReason.STARTUP_CATCHUP.value,
        }

        if not is_automatic:
            tr = self.controller.i18n.tr
            message = self.controller.i18n.operation_message(result)

            if result.success and (result.result_data or {}).get("leftovers"):
                self._show_plan_delete_leftovers(result)
            elif result.success:
                QMessageBox.information(
                    self,
                    tr("dialog.done"),
                    message,
                )
            else:
                QMessageBox.warning(
                    self,
                    tr("dialog.operation_failed"),
                    message + "\n\n" + tr(
                        "dialog.error_code",
                        code=result.error_code or "UNKNOWN",
                    ),
                )

        if (
            result.success
            and result.result_code == "PLAN_DELETE_SUCCEEDED"
        ):
            self.current_plan_id = None
            self.clear_detail()
            self.stack.setCurrentWidget(self.empty_view)

        if is_automatic:
            self.request_refresh()
        else:
            # Explicit user commands keep their immediate UI contract.
            self.refresh()

        if self.tray_runtime is not None:
            self.tray_runtime.refresh()

    def start_recovery(self, plan_id: str) -> None:
        if self.controller.repo.plan_deletion_job(plan_id):
            self.delete_plan_data(plan_id)
            return
        tr = self.controller.i18n.tr
        answer = QMessageBox.question(
            self, tr("health.recover"), tr("health.recover_question"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.queue_recovery(plan_id)
            self.show_progress(tr("progress.recover"))
        except AppError as exc:
            QMessageBox.warning(
                self, tr("dialog.operation_failed"),
                self.controller.i18n.error_message(exc),
            )

    def queue_backup(self, plan_id: str, force: bool = False, _attempt: int = 0) -> None:
        tr = self.controller.i18n.tr
        try:
            self.controller.queue_backup(plan_id, force)
            self.show_progress(tr("progress.backup"))
        except AppError as exc:
            if exc.code == "OPERATION_ALREADY_RUNNING" and self._defer_backup_request(plan_id, force, _attempt):
                return
            QMessageBox.warning(
                self,
                tr("dialog.cannot_start_backup"),
                self.controller.i18n.error_message(exc),
            )

    def _defer_backup_request(self, plan_id: str, force: bool, attempt: int) -> bool:
        """A short background query (health, version list) may still be running right after a plan was created.
        Wait for it quietly instead of showing "another operation is running". True when the request was handled."""
        controller = self.controller
        active = controller.active_operation_id
        if not active or getattr(controller, "_shutdown_requested", False):
            return False
        if active not in getattr(controller, "_background_queries", {}):
            try:
                if controller.repo.operation_plan_id(active) == plan_id:
                    self.show_progress(controller.i18n.tr("progress.backup"))
                    return True
            except AppError:
                pass
            return False
        if attempt >= BACKUP_DEFER_MAX_ATTEMPTS:
            return False
        QTimer.singleShot(BACKUP_DEFER_DELAY_MS, lambda: self.queue_backup(plan_id, force, attempt + 1))
        return True

    def queue_verify(self, snapshot_id: str) -> None:
        tr = self.controller.i18n.tr
        try:
            self.controller.queue_verify(snapshot_id)
            self.show_progress(tr("progress.verify"))
        except AppError as exc:
            QMessageBox.warning(
                self,
                tr("dialog.cannot_verify"),
                self.controller.i18n.error_message(exc),
            )

    def verify_latest(self, plan_id: str) -> None:
        snapshots = self.controller.repo.available_snapshots(plan_id)
        if not snapshots:
            tr = self.controller.i18n.tr
            QMessageBox.information(
                self,
                tr("dialog.no_version"),
                tr("dialog.no_version_detail"),
            )
            return
        self.queue_verify(snapshots[0].id)

    def queue_restore(self, snapshot_id: str) -> None:
        tr = self.controller.i18n.tr
        try:
            self.controller.queue_restore(snapshot_id)
            self.show_progress(tr("progress.restore"))
        except AppError as exc:
            QMessageBox.warning(
                self,
                tr("dialog.cannot_restore"),
                self.controller.i18n.error_message(exc),
            )

    def replace_original_file(self, snapshot_id: str) -> None:
        tr = self.controller.i18n.tr

        try:
            snapshot = self.controller.repo.snapshot(snapshot_id)
            if snapshot.state != "committed":
                raise AppError(
                    "SNAPSHOT_NOT_COMMITTED",
                    "The version is unavailable.",
                )
            if snapshot.source_kind != SourceKind.FILE.value:
                raise AppError(
                    "RESTORE_REPLACE_INVALID",
                    "Replacing a folder is not supported.",
                )

            source = Path(snapshot.source_path)
            if not source.is_file() or source.is_symlink():
                raise AppError(
                    "RESTORE_REPLACE_INVALID",
                    "The original file is missing or unavailable.",
                )
        except AppError as exc:
            QMessageBox.warning(
                self,
                tr("dialog.cannot_restore"),
                self.controller.i18n.error_message(exc),
            )
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("replace_file.title"))
        box.setText(
            tr("replace_file.question", path=str(source))
        )
        box.setStandardButtons(QMessageBox.StandardButton.Cancel)
        cancel_button = box.button(QMessageBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText(tr("common.cancel"))
        confirm = box.addButton(
            tr("replace_file.confirm"),
            QMessageBox.ButtonRole.DestructiveRole,
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        if box.clickedButton() is not confirm:
            return

        try:
            self.controller.queue_replace_original_file(snapshot_id)
            self.show_progress(tr("progress.restore"))
        except AppError as exc:
            QMessageBox.warning(
                self,
                tr("dialog.cannot_restore"),
                self.controller.i18n.error_message(exc),
            )

    def suggested_restore_folder(self, snapshot_id: str) -> str:
        """Folder offered for restoring: the last used one, else <Documents>\\AmviBackUp restored."""
        try:
            snapshot = self.controller.repo.snapshot(snapshot_id)
        except AppError:
            return ""
        return suggest_restore_folder(self.controller.repo, snapshot)

    def prepare_restore_folder(self, text: str, snapshot_id: str) -> None:
        """Creates a new restore folder only outside the protected source and storage."""
        value = (text or "").strip()
        if not value:
            raise AppError("RESTORE_DESTINATION_MISSING", dictionary_text("error.RESTORE_DESTINATION_MISSING"))
        snapshot = self.controller.repo.snapshot(snapshot_id)
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise AppError("RESTORE_DESTINATION_NOT_FOLDER", dictionary_text("error.RESTORE_DESTINATION_NOT_FOLDER"))
        if not restore_folder_is_allowed(path, snapshot):
            code = "RESTORE_DESTINATION_IN_STORAGE" if PathPolicy.inside(path, Path(snapshot.storage_path)) else "RESTORE_DESTINATION_IN_SOURCE"
            raise AppError(code, dictionary_text(f"error.{code}"))
        if not path.exists():
            try:
                path.mkdir(exist_ok=True)
            except OSError as exc:
                raise AppError("RESTORE_DESTINATION_NOT_FOLDER", dictionary_text("error.RESTORE_DESTINATION_NOT_FOLDER"), str(exc)) from exc

    def remember_restore_folder(self, text: str) -> None:
        value = (text or "").strip()
        if not value:
            return
        try:
            self.controller.repo.set_setting(SETTING_LAST_RESTORE_FOLDER, value)
        except AppError:
            logging.warning("Cannot remember the restore folder", exc_info=True)

    def restore_to_directory(self, snapshot_id: str) -> None:
        tr = self.controller.i18n.tr
        start = self.suggested_restore_folder(snapshot_id)
        if start and not Path(start).is_dir():
            start = str(Path(start).parent)
        directory = QFileDialog.getExistingDirectory(
            self, tr("dialog.choose_restore_directory"), start
        )
        if not directory:
            return
        try:
            self.prepare_restore_folder(directory, snapshot_id)
            self.controller.queue_restore_to_directory(snapshot_id, directory)
            self.remember_restore_folder(directory)
            self.show_progress(tr("progress.restore"))
        except AppError as exc:
            QMessageBox.warning(
                self, tr("dialog.cannot_restore"),
                self.controller.i18n.error_message(exc),
            )

    def restore_selected_files(self, snapshot_id: str, destination: str = "", confirm: bool = False) -> None:
        tr = self.controller.i18n.tr
        if destination:
            self._pending_restore_destination19 = destination
        destination = destination or getattr(self, "_pending_restore_destination19", "")
        self._pending_restore_destination19 = ""

        if self.controller._shutdown_requested:
            return

        try:
            cached = self.controller.cached_snapshot_files(
                snapshot_id
            )

            if cached is None:
                self._pending_restore_destination19 = destination
                self._pending_restore_confirm19 = confirm
                op = self.controller.queue_snapshot_files(
                    snapshot_id
                )
                self._pending_snapshot_files = (
                    op,
                    snapshot_id,
                )
                self.show_progress(
                    tr("progress.snapshot_files")
                )
                return

            dialog = RestoreSelectedFilesDialog(
                self.controller,
                snapshot_id,
                self,
            )
            if destination:
                dialog.destination_edit.setText(destination)
            else:
                dialog.destination_edit.setText(
                    self.suggested_restore_folder(snapshot_id)
                )

        except AppError as exc:
            QMessageBox.warning(
                self,
                tr("dialog.cannot_restore"),
                self.controller.i18n.error_message(exc),
            )
            return

        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return

            if confirm and not self.confirm_restore_summary19(
                    snapshot_id, "selected", dialog.destination_edit.text().strip(),
                    len(dialog.selected_relative_paths)):
                return

            self.prepare_restore_folder(dialog.destination_edit.text(), snapshot_id)
            self.controller.queue_restore_selected_files(
                snapshot_id,
                dialog.selected_relative_paths,
                dialog.destination_edit.text(),
            )
            self.remember_restore_folder(dialog.destination_edit.text())
            self.show_progress(tr("progress.restore"))

        except AppError as exc:
            QMessageBox.warning(
                self,
                tr("dialog.cannot_restore"),
                self.controller.i18n.error_message(exc),
            )

        finally:
            dialog.deleteLater()

    def _snapshot_files_loaded(
        self,
        result: OperationResult,
    ) -> None:
        pending = self._pending_snapshot_files

        if (
            pending is None
            or result.operation_id != pending[0]
        ):
            return

        self._pending_snapshot_files = None

        if self.controller._shutdown_requested:
            return

        self.progress_frame.hide()

        if result.success:
            self.restore_selected_files(pending[1], "", self._pending_restore_confirm19)

        elif result.error_code != "OPERATION_CANCELLED":
            tr = self.controller.i18n.tr
            QMessageBox.warning(
                self,
                tr("dialog.cannot_restore"),
                self.controller.i18n.operation_message(result)
                + "\n\n"
                + tr(
                    "dialog.error_code",
                    code=result.error_code or "UNKNOWN",
                ),
            )

    def queue_delete(self, snapshot_ids: list[str]) -> None:
        tr = self.controller.i18n.tr
        try:
            self.controller.queue_delete(snapshot_ids)
            self.show_progress(tr("progress.delete"))
        except AppError as exc:
            QMessageBox.warning(
                self,
                tr("dialog.cannot_delete"),
                self.controller.i18n.error_message(exc),
            )

    def set_pin(self, snapshot_id: str, pinned: bool) -> None:
        try:
            self.controller.set_pin(snapshot_id, pinned)
            self.refresh()
        except AppError as exc:
            QMessageBox.warning(
                self,
                self.controller.i18n.tr("dialog.cannot_pin"),
                self.controller.i18n.error_message(exc),
            )

    def _show_plan_delete_leftovers(self, result: OperationResult) -> None:
        tr = self.controller.i18n.tr
        data = result.result_data or {}
        leftovers = [str(item) for item in data.get("leftovers") or []]
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("plan_delete.leftovers_title"))
        layout = QVBoxLayout(dialog)
        summary = QLabel(tr(
            "plan_delete.leftovers_body",
            deleted=data.get("deleted_files", 0),
            failed=data.get("failed_files", len(leftovers)),
        ))
        summary.setWordWrap(True)
        layout.addWidget(summary)
        listing = QTextEdit()
        listing.setReadOnly(True)
        listing.setPlainText("\n".join(leftovers))
        layout.addWidget(listing)
        buttons = QHBoxLayout()
        open_button = QPushButton(tr("plan_delete.open_folder"))
        folder = str(data.get("folder") or "")
        if not folder and leftovers:
            folder = str(Path(leftovers[0]).parent)
        open_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)) if folder else None
        )
        close_button = QPushButton(tr("plan_delete.close"))
        close_button.clicked.connect(dialog.accept)
        buttons.addWidget(open_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        dialog.exec()

    def delete_plan_data(self, plan_id: str) -> None:
        tr = self.controller.i18n.tr
        try:
            preview = self.controller.plan_deletion_preview(plan_id)
        except AppError as exc:
            QMessageBox.warning(self, tr("dialog.operation_failed"), self.controller.i18n.error_message(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("plan_delete.title"))
        layout = QVBoxLayout(dialog)
        summary = QLabel(tr(
            "plan_delete.brief",
            name=preview.display_name,
            plan_id=preview.plan_id,
            source=preview.source_path,
            storage=preview.storage_path,
            count=preview.snapshot_count,
            size=fmt_size(preview.stored_size_bytes),
            pinned=preview.pinned_count,
        ))
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(summary)
        if preview.has_problems:
            problem = QLabel(tr("plan_delete.problems"))
            problem.setWordWrap(True)
            layout.addWidget(problem)
        if "source_overlap" in preview.warnings:
            overlap = QLabel(tr("plan_delete.overlap"))
            overlap.setWordWrap(True)
            layout.addWidget(overlap)
        details = QLabel(tr("plan_delete.details", pinned=preview.pinned_count))
        details.setWordWrap(True)
        details.setVisible(False)
        layout.addWidget(details)
        toggle = QPushButton(tr("plan_delete.details_show"))

        def toggle_details() -> None:
            details.setVisible(not details.isVisible())
            toggle.setText(tr("plan_delete.details_hide") if details.isVisible() else tr("plan_delete.details_show"))

        toggle.clicked.connect(toggle_details)
        layout.addWidget(toggle)
        buttons = QHBoxLayout()
        cancel = QPushButton(tr("plan_delete.cancel"))
        confirm = QPushButton(tr("plan_delete.confirm"))
        cancel.clicked.connect(dialog.reject)
        confirm.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.controller.queue_delete_plan_data(plan_id, preview.token, bool(preview.pinned_count))
            self.show_progress(tr("plan_delete.progress"))
        except AppError as exc:
            QMessageBox.warning(self, tr("dialog.operation_failed"), self.controller.i18n.error_message(exc))
    def archive_plan(self, plan_id: str) -> None:
        tr = self.controller.i18n.tr
        answer = QMessageBox.question(
            self,
            tr("dialog.archive_title"),
            tr("dialog.archive_question"),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.archive_plan(plan_id)
            self.refresh()
        except AppError as exc:
            QMessageBox.warning(
                self,
                tr("dialog.cannot_archive"),
                self.controller.i18n.error_message(exc),
            )

    def edit_plan(self, plan_id: str) -> None:
        try:
            plan = self.controller.repo.plan_details(plan_id)
        except AppError as exc:
            QMessageBox.warning(
                self,
                self.controller.i18n.tr("dialog.plan_unavailable"),
                self.controller.i18n.error_message(exc),
            )
            return
        dialog = PlanEditor(self.controller, plan, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def new_plan(self, _checked: bool = False, advanced: bool | None = None, source: Path | None = None) -> None:
        dialog = PlanEditor(self.controller, parent=self, start_advanced=advanced)
        if source is not None:
            dialog._apply_source_drop(source)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            created, first = dialog.created_plan_id, dialog.start_first_backup
        finally:
            dialog.deleteLater()
        self.refresh()
        if created:
            self.select_plan_by_id(created)
        if created and first:
            self.queue_backup(created)

    def new_plan_advanced(self, _checked: bool = False) -> None:
        self.new_plan(advanced=True)

    def open_health_filter(
        self,
        health_state: str,
    ) -> None:
        if health_state == HealthState.ERROR.value:
            filter_name = "error"
        elif health_state == HealthState.ATTENTION.value:
            filter_name = "warning"
        else:
            filter_name = "all"

        dialog = HealthCenterDialog(
            self.controller,
            self,
        )
        dialog.set_filter(filter_name)
        dialog.exec()
        self.refresh()

    def queue_test_restore19(self, snapshot_id, mode="full"):
        tr = self.controller.i18n.tr
        try:
            self.controller.queue_test_restore(snapshot_id, mode)
            self.show_progress(tr("recovery.running"))
        except AppError as exc:
            QMessageBox.warning(self, tr("dialog.cannot_restore"),
                                self.controller.i18n.error_message(exc))

    def _place_progress19(self) -> None:
        height = min(150, self.height())
        self.progress_frame.setGeometry(0, self.height() - height, self.width(), height)

    def _auto_restore_tick19(self) -> None:
        if self.controller._shutdown_requested:
            return
        try:
            candidate = self.controller.auto_restore_test_candidate()
            if candidate:
                self.controller.queue_test_restore(candidate[0], candidate[1])
                self.show_progress(self.controller.i18n.tr("recovery.running"))
        except AppError:
            pass
        except Exception:
            logging.exception("Automatic restore test failed to start")

    def overview_state19(self) -> dict:
        repo, service = self.controller.repo, self.controller.restore_test_service
        summaries = repo.plan_summaries(False)
        state = {"total": len(summaries), "last_backup": 0, "verified": 0, "tested": 0,
                 "verified_n": 0, "tested_n": 0, "no_copy": [], "unverified": [], "untested": [],
                 "problems": [], "attention": 0, "samples": 0}
        for summary in summaries:
            snapshots = repo.available_snapshots(summary.id)
            if not snapshots:
                state["no_copy"].append(summary)
                state["attention"] += 1
                continue
            latest = snapshots[0]
            flagged = False
            state["last_backup"] = max(state["last_backup"], int(latest.created_at_utc_ms or 0))
            if latest.verified_at_utc_ms:
                state["verified_n"] += 1
                state["verified"] = max(state["verified"], int(latest.verified_at_utc_ms))
            else:
                state["unverified"].append((summary, latest)); flagged = True
            status = service.status(latest)
            if status["proof"]:
                state["tested_n"] += 1
                state["tested"] = max(state["tested"], int(status["proof"]["tested_at_utc_ms"]))
                if status["proof"].get("scope", "full") == "sample":
                    state["samples"] += 1
            else:
                state["untested"].append((summary, latest)); flagged = True
            if status["state"] in {"failed", "proven_then_failed"} and status["attempt"]:
                state["problems"].append((summary, status["attempt"])); flagged = True
            state["attention"] += 1 if flagged else 0
        if not summaries:
            state["action"] = "create"
        elif state["no_copy"]:
            state["action"] = "first"
        elif state["problems"]:
            state["action"] = "problem"
        elif state["unverified"]:
            state["action"] = "verify"
        elif state["untested"]:
            state["action"] = "test"
        else:
            state["action"] = "ok"
        return state

    def overview_text19(self, state: dict | None = None) -> str:
        tr = self.controller.i18n.tr
        state = state or self.overview_state19()
        total = state["total"]
        lines = [tr("overview.attention", n=state["attention"], total=total),
                 tr("recovery.overview.last_backup", time=fmt_time(state["last_backup"])),
                 tr("recovery.overview.verify", n=state["verified_n"], total=total, time=fmt_time(state["verified"])),
                 tr("recovery.overview.restore", n=state["tested_n"], total=total, time=fmt_time(state["tested"]))]
        if state["samples"]:
            lines.append(tr("overview.samples", n=state["samples"]))
        for summary, attempt in state["problems"][:3]:
            reason = tr("overview.reason." + str(attempt.get("status")))
            lines.append(tr("overview.problem", name=summary.display_name, reason=reason))
        if self.controller.active_operation_id:
            lines.append(tr("recovery.overview.running"))
        lines.append(tr("overview.a." + state["action"]))
        return "\n".join(lines)

    def make_tile19(self, key: str, tip_key: str, callback, primary: bool) -> QPushButton:
        tr = self.controller.i18n.tr
        tile = QPushButton()
        tile.setCursor(Qt.CursorShape.PointingHandCursor)
        tile.setMinimumHeight(88)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip(tr(tip_key))
        tile.clicked.connect(lambda _checked=False: callback())
        if primary:
            tile.setStyleSheet(
                "QPushButton{border:none;border-radius:10px;background:#4f6bff;}"
                "QPushButton:hover{background:#6a82ff;}"
                "QLabel{background:transparent;color:#ffffff;}")
            sub_color = "#e1e8ff"
        else:
            tile.setStyleSheet(
                "QPushButton{border:1px solid #3a475e;border-radius:10px;background:#222c3d;}"
                "QPushButton:hover{background:#2c3950;border-color:#5368bb;}"
                "QPushButton:disabled{background:#1a202c;border-color:#2d3748;}"
                "QPushButton:disabled QLabel{color:#6b7c96;}"
                "QLabel{background:transparent;color:#edf2f8;}")
            sub_color = "#9aa8bc"
        layout = QVBoxLayout(tile)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(4)
        layout.addStretch()
        title = QLabel(tr(f"{key}.t"))
        title.setStyleSheet("font-size:16px;font-weight:700;")
        subtitle = QLabel(tr(f"{key}.s"))
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"font-size:12px;color:{sub_color};")
        for label in (title, subtitle):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(label)
        layout.addStretch()
        return tile

    def refresh_overview_sets19(self) -> None:
        clear_layout_recursively(self.overview_sets19)
        tr = self.controller.i18n.tr
        repo = self.controller.repo
        for summary in repo.plan_summaries(False):
            health = self.controller.cached_plan_health(summary.id)
            snapshots = repo.available_snapshots(summary.id)
            color = StatusBadge.COLORS.get(health.state, "#9aa8bc")
            card = QFrame()
            card.setProperty("role", "card")
            row = QHBoxLayout(card)
            row.setContentsMargins(12, 10, 12, 10)
            row.setSpacing(12)
            stripe = QFrame(); stripe.setFixedWidth(4)
            stripe.setStyleSheet(f"background:{color};border-radius:2px;")
            row.addWidget(stripe)
            info = QVBoxLayout(); info.setSpacing(2)
            name = QLabel(summary.display_name); name.setProperty("role", "subtitle")
            last = QLabel(tr("datasets.last", time=fmt_time(snapshots[0].created_at_utc_ms) if snapshots else tr("datasets.never")))
            last.setProperty("role", "muted")
            info.addWidget(name); info.addWidget(last)
            row.addLayout(info, 1)
            row.addWidget(StatusBadge(health.state, tr(f"health.state.{health.state}")))
            open_button = self.make_button(tr("datasets.open"), lambda _c=False, sid=summary.id: self.show_plan_detail(sid), role="ghost")
            copy_button = self.make_button(tr("datasets.copy"), lambda _c=False, sid=summary.id: self.queue_backup(sid))
            for button in (open_button, copy_button):
                button.setMinimumHeight(32)
            row.addWidget(open_button)
            row.addWidget(copy_button)
            self.overview_sets19.addWidget(card)

    def overview_headline_text19(self, state: dict) -> str:
        tr = self.controller.i18n.tr
        if state["total"] == 0:
            return tr("home.head.none")
        if state["problems"]:
            return tr("home.head.problem")
        if state["no_copy"]:
            return tr("home.head.nocopy")
        if state["action"] != "ok":
            return tr("home.head.need")
        return tr("home.head.ok")

    def set_nav19(self, nav_id: str) -> None:
        buttons = getattr(self, "nav_buttons19", {})
        for key in ("overview", "data"):
            if key in buttons:
                buttons[key].setChecked(key == nav_id)

    def quick_backup19(self) -> None:
        summaries = self.controller.repo.plan_summaries(False)
        if not summaries:
            self.guided_setup19(); return
        if len(summaries) == 1:
            self.queue_backup(summaries[0].id); return
        menu = QMenu(self)
        for summary in summaries:
            menu.addAction(summary.display_name, lambda _c=False, sid=summary.id: self.queue_backup(sid))
        menu.exec(self.tile_backup19.mapToGlobal(self.tile_backup19.rect().bottomLeft()))

    def run_recommended19(self) -> None:
        tr = self.controller.i18n.tr
        state = self.overview_state19()
        action = state["action"]
        try:
            if action == "create":
                self.guided_setup19()
            elif action == "first" and state["no_copy"]:
                self.select_plan_by_id(state["no_copy"][0].id)
            elif action == "problem" and state["problems"]:
                self.show_recovery_center19()
            elif action == "verify" and state["unverified"]:
                self.controller.queue_verify(state["unverified"][0][1].id)
                self.show_progress(tr("overview.running_verify"))
            elif action == "test" and state["untested"]:
                self.queue_test_restore19(state["untested"][0][1].id, "full")
        except AppError as exc:
            QMessageBox.warning(self, tr("recovery.title"), self.controller.i18n.error_message(exc))

    def show_overview19(self) -> None:
        self.current_plan_id = None
        self.refresh()
        self.stack.setCurrentWidget(
            self.overview_view if self.controller.repo.plan_summaries(False) else self.empty_view)
        self.set_nav19("overview")

    def show_my_data19(self) -> None:
        if not self.current_plan_id:
            summaries = self.controller.repo.plan_summaries(False)
            if not summaries:
                self.show_overview19(); return
            self.current_plan_id = summaries[0].id
        self.show_plan_detail(self.current_plan_id)
        self.set_nav19("data")

    def _protected_plan_for_path(self, path: Path) -> str | None:
        try:
            target = Path(os.path.abspath(path))
        except OSError:
            return None
        for summary in self.controller.repo.plan_summaries(False):
            source = Path(summary.source_path)
            try:
                source_abs = Path(os.path.abspath(source))
            except OSError:
                continue
            same = os.path.normcase(str(source_abs)) == os.path.normcase(str(target))
            inside = source_abs.is_dir() and PathPolicy.inside(target, source_abs)
            if same or inside:
                return summary.id
        return None

    def _open_protection_for_path(self, path: Path) -> None:
        tr = self.controller.i18n.tr
        plan_id = self._protected_plan_for_path(path)
        if plan_id:
            answer = QMessageBox.question(
                self, tr("drop.existing_title"), tr("drop.existing_question"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.select_plan_by_id(plan_id)
                return
        self.new_plan(source=path)


    def _drop_many_sources(self, paths: list[Path]) -> None:
        tr = self.controller.i18n.tr
        answer = QMessageBox.question(
            self, tr("drop.many_title"), tr("drop.many_question", count=len(paths)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for path in paths:
            self._open_protection_for_path(path)

    def guided_setup19(self):
        self.new_plan()

    def show_recovery_center19(self):
        from PySide6.QtWidgets import QMenu
        tr = self.controller.i18n.tr
        repo, service, controller = self.controller.repo, self.controller.restore_test_service, self.controller
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("recovery.title"))
        dialog.resize(980, 640)
        layout = QVBoxLayout(dialog)
        summaries = repo.plan_summaries(False)
        if not summaries:
            layout.addWidget(QLabel(tr("recovery.empty_backup")))
            close = QPushButton(tr("common.close")); close.clicked.connect(dialog.accept); layout.addWidget(close)
            dialog.exec(); return

        top = QHBoxLayout()
        top.addWidget(QLabel(tr("recovery.center.set")))
        set_combo = QComboBox()
        for summary in summaries:
            set_combo.addItem(summary.display_name, summary.id)
        top.addWidget(set_combo, 1)
        layout.addLayout(top)
        preselect = getattr(self, "_recovery_preselect19", None)
        self._recovery_preselect19 = None
        if preselect:
            found = set_combo.findData(preselect)
            if found >= 0:
                set_combo.setCurrentIndex(found)

        settings = controller.restore_test_settings19()
        auto_row = QHBoxLayout()
        auto_check = QCheckBox(tr("recovery.center.auto")); auto_check.setChecked(settings["enabled"])
        auto_days = QSpinBox(); auto_days.setRange(1, 90); auto_days.setValue(settings["days"])
        auto_limit = QSpinBox(); auto_limit.setRange(16, 1024 * 1024)
        auto_limit.setSuffix(" " + tr("overview.mib")); auto_limit.setValue(max(16, settings["max_bytes"] // (1024 * 1024)))
        auto_row.addWidget(auto_check); auto_row.addWidget(QLabel(tr("recovery.center.auto_days"))); auto_row.addWidget(auto_days)
        auto_row.addWidget(QLabel(tr("recovery.limit.limit"))); auto_row.addWidget(auto_limit)
        auto_row.addStretch()
        layout.addLayout(auto_row)
        help_label = QLabel(tr("recovery.center.auto_help")); help_label.setProperty("role", "muted"); help_label.setWordWrap(True)
        layout.addWidget(help_label)

        def save_auto() -> None:
            try:
                controller.save_restore_test_settings19(auto_check.isChecked(), auto_days.value(), auto_limit.value())
            except AppError as exc:
                QMessageBox.warning(dialog, tr("recovery.title"), controller.i18n.error_message(exc))
        auto_check.toggled.connect(lambda _checked=False: save_auto())
        auto_days.valueChanged.connect(lambda _value=0: save_auto())
        auto_limit.valueChanged.connect(lambda _value=0: save_auto())

        tree = QTreeWidget()
        tree.setColumnCount(5)
        tree.setHeaderLabels([tr("recovery.center.time"), tr("recovery.center.files"), tr("recovery.center.size"),
                              tr("recovery.center.integrity"), tr("recovery.center.test")])
        tree.setColumnWidth(0, 230)
        tree.setColumnWidth(3, 110)
        tree.setColumnWidth(4, 150)
        layout.addWidget(tree, 1)
        detail = QLabel(); detail.setWordWrap(True); detail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(detail)
        snapshots_by_id: dict[str, Any] = {}

        def size_text(value: int) -> str:
            return fmt_size(int(value or 0))

        def test_text(snapshot) -> str:
            status = service.status(snapshot)
            proof, attempt = status["proof"], status["attempt"]
            def state_name(item) -> str:
                return tr("recovery.status." + str(item.get("status")))
            if status["state"] == "proven":
                key = "recovery.result.sample" if proof.get("scope", "full") == "sample" else "recovery.result.full"
                return tr(key, time=fmt_time(proof["tested_at_utc_ms"]),
                          files=proof.get("files", "?"), total=proof.get("total_files", proof.get("files", "?")),
                          size=size_text(proof.get("bytes", 0)),
                          seconds=round(proof.get("duration_ms", 0) / 1000, 1))
            if status["state"] == "proven_then_failed":
                return tr("recovery.test.failed_after", old=fmt_time(proof["tested_at_utc_ms"]),
                          new=fmt_time(attempt["finished_at_utc_ms"]), status=state_name(attempt))
            if attempt:
                return tr("recovery.test.failed", new=fmt_time(attempt["finished_at_utc_ms"]), status=state_name(attempt))
            return tr("recovery.test.none")

        def integrity_text(snapshot) -> str:
            return (tr("recovery.item.done", time=fmt_time(snapshot.verified_at_utc_ms))
                    if snapshot.verified_at_utc_ms else tr("recovery.item.none"))

        def short_test(snapshot) -> str:
            marks = {"proven": "mark.passed", "proven_then_failed": "mark.recheck", "failed": "mark.failed"}
            key = marks.get(service.status(snapshot)["state"])
            return tr(key) if key else "—"

        def load_points() -> None:
            tree.clear(); snapshots_by_id.clear(); detail.setText("")
            plan_id = set_combo.currentData()
            snapshots = repo.available_snapshots(plan_id)
            if not snapshots:
                detail.setText(tr("recovery.center.no_points")); return
            groups: dict[str, QTreeWidgetItem] = {}
            for snapshot in snapshots:
                day = datetime.fromtimestamp(snapshot.created_at_utc_ms / 1000).strftime("%d.%m.%Y")
                if day not in groups:
                    groups[day] = QTreeWidgetItem([day]); tree.addTopLevelItem(groups[day]); groups[day].setExpanded(len(groups) == 1)
                item = QTreeWidgetItem([fmt_time(snapshot.created_at_utc_ms), str(snapshot.file_count),
                                        size_text(snapshot.logical_size_bytes),
                                        tr("mark.yes") if snapshot.verified_at_utc_ms else "—", short_test(snapshot)])
                item.setData(0, Qt.ItemDataRole.UserRole, snapshot.id)
                groups[day].addChild(item)
                snapshots_by_id[snapshot.id] = snapshot
            first_group = tree.topLevelItem(0)
            if first_group is not None and first_group.childCount():
                tree.setCurrentItem(first_group.child(0))

        def current_snapshot():
            item = tree.currentItem()
            sid = item.data(0, Qt.ItemDataRole.UserRole) if item else None
            return snapshots_by_id.get(sid) if sid else None

        def update_detail() -> None:
            snapshot = current_snapshot()
            detail.setText("" if snapshot is None else tr(
                "recovery.center.detail", created=fmt_time(snapshot.created_at_utc_ms),
                integrity=integrity_text(snapshot), test=test_text(snapshot)))
        tree.currentItemChanged.connect(lambda *_args: update_detail())
        set_combo.currentIndexChanged.connect(lambda _index: load_points())

        def act(callback, confirm: bool = False) -> None:
            snapshot = current_snapshot()
            if snapshot is None:
                QMessageBox.information(dialog, tr("recovery.title"), tr("recovery.center.pick")); return
            if confirm and QMessageBox.question(
                    dialog, tr("recovery.title"),
                    tr("recovery.center.confirm", files=snapshot.file_count, time=fmt_time(snapshot.created_at_utc_ms)),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            dialog.accept()
            callback(snapshot.id)

        buttons = QHBoxLayout()
        restore_all = QPushButton(tr("restore_wizard.button")); restore_all.setProperty("primary", True)
        restore_all.clicked.connect(lambda: act(self.restore_wizard19))
        run_test = QPushButton(tr("recovery.center.run_test"))
        test_menu = QMenu(run_test)
        test_menu.addAction(tr("recovery.limit.test_sample"), lambda: act(lambda sid: self.queue_test_restore19(sid, "sample")))
        test_menu.addAction(tr("recovery.limit.test_full"), lambda: act(lambda sid: self.queue_test_restore19(sid, "full")))
        run_test.setMenu(test_menu)
        more = QPushButton(tr("recovery.center.more")); menu = QMenu(more)
        menu.addAction(tr("recovery.center.verify"), lambda: act(self.queue_verify))
        menu.addAction(tr("recovery.center.restore_all"), lambda: act(self.restore_to_directory, True))
        menu.addAction(tr("recovery.center.pick_files"), lambda: act(self.restore_selected_files))
        menu.addAction(tr("recovery.center.replace"), lambda: act(self.replace_original_file))
        menu.addAction(tr("recovery.center.open"), lambda: (dialog.accept(), self.show_plan_detail(set_combo.currentData())))
        more.setMenu(menu)
        close = QPushButton(tr("common.close")); close.clicked.connect(dialog.accept)
        for widget in (restore_all, run_test, more):
            buttons.addWidget(widget)
        buttons.addStretch(); buttons.addWidget(close)
        layout.addLayout(buttons)
        load_points()
        dialog.exec()

    def confirm_restore_summary19(self, snapshot_id: str, scope: str, destination: str,
                                  selected_count: int | None = None) -> bool:
        tr = self.controller.i18n.tr
        try:
            snapshot = self.controller.repo.snapshot(snapshot_id)
            plan = self.controller.repo.plan_details(snapshot.plan_id)
        except AppError as exc:
            QMessageBox.warning(self, tr("recovery.title"), self.controller.i18n.error_message(exc))
            return False
        if scope == "selected":
            what = tr("restore_wizard.what_selected", count=selected_count or 0)
        else:
            what = tr("restore_wizard.what_all", files=snapshot.file_count)
        if scope == "replace":
            where, note = str(snapshot.source_path), tr("restore_wizard.note_replace")
        else:
            where, note = destination, tr("restore_wizard.note_safe")
        answer = QMessageBox.question(
            self, tr("restore_wizard.summary_title"),
            tr("restore_wizard.summary", name=plan.display_name, time=fmt_time(snapshot.created_at_utc_ms),
               what=what, where=where, note=note),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def restore_wizard19(self, snapshot_id: str) -> None:
        """Scope -> destination -> consequences summary -> start. Safe separate folder is default."""
        tr = self.controller.i18n.tr
        try:
            snapshot = self.controller.repo.snapshot(snapshot_id)
            if snapshot.state != "committed":
                raise AppError("SNAPSHOT_NOT_COMMITTED", "The version is not available for restoring.")
        except AppError as exc:
            QMessageBox.warning(self, tr("recovery.title"), self.controller.i18n.error_message(exc))
            return
        is_folder = snapshot.source_kind == SourceKind.FOLDER.value
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("restore_wizard.title"))
        dialog.resize(640, 360)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(tr("restore_wizard.point", time=fmt_time(snapshot.created_at_utc_ms),
                                   files=snapshot.file_count)))
        scope_all = QRadioButton(tr("restore_wizard.scope_all")); scope_all.setChecked(True)
        scope_selected = QRadioButton(tr("restore_wizard.scope_selected"))
        scope_selected.setEnabled(is_folder)
        group = QButtonGroup(dialog); group.addButton(scope_all); group.addButton(scope_selected)
        layout.addWidget(scope_all); layout.addWidget(scope_selected)
        layout.addWidget(QLabel(tr("restore_wizard.where")))
        dest_new = QRadioButton(tr("restore_wizard.dest_new")); dest_new.setChecked(True)
        dest_replace = QRadioButton(tr("restore_wizard.dest_replace"))
        can_replace = False
        if snapshot.source_kind == SourceKind.FILE.value:
            source = Path(snapshot.source_path)
            can_replace = source.is_absolute() and source.is_file() and not source.is_symlink()
        dest_replace.setEnabled(can_replace)
        dest_group = QButtonGroup(dialog); dest_group.addButton(dest_new); dest_group.addButton(dest_replace)
        layout.addWidget(dest_new)
        row = QHBoxLayout()
        dest_edit = QLineEdit(); row.addWidget(dest_edit, 1)
        dest_edit.setText(self.suggested_restore_folder(snapshot_id))
        browse = QPushButton(tr("setup.wizard.browse")); row.addWidget(browse)
        layout.addLayout(row)
        folder_hint = QLabel(tr("restore_wizard.default_hint")); folder_hint.setProperty("role", "muted"); folder_hint.setWordWrap(True)
        layout.addWidget(folder_hint)
        layout.addWidget(dest_replace)
        if not can_replace:
            note = QLabel(tr("restore_wizard.replace_unavailable")); note.setProperty("role", "muted"); note.setWordWrap(True)
            layout.addWidget(note)

        def choose() -> None:
            start = dest_edit.text().strip()
            if start and not Path(start).is_dir():
                start = str(Path(start).parent)
            value = QFileDialog.getExistingDirectory(dialog, tr("dialog.choose_restore_directory"), start)
            if value:
                dest_edit.setText(value); dest_new.setChecked(True)
        browse.clicked.connect(choose)
        PathDropTarget(
            dialog, self.controller.i18n, "folder",
            lambda path: (dest_edit.setText(str(path)), dest_new.setChecked(True)),
            title=tr("drop.folder_title"), detail=tr("drop.folder_detail"),
        )

        def sync() -> None:
            dest_edit.setEnabled(dest_new.isChecked()); browse.setEnabled(dest_new.isChecked())
            if dest_replace.isChecked():
                scope_all.setChecked(True)
            scope_selected.setEnabled(is_folder and not dest_replace.isChecked())
        dest_new.toggled.connect(lambda _c=False: sync()); dest_replace.toggled.connect(lambda _c=False: sync())
        layout.addStretch()
        buttons = QHBoxLayout(); buttons.addStretch()
        cancel = QPushButton(tr("common.cancel")); cancel.clicked.connect(dialog.reject)
        go = QPushButton(tr("restore_wizard.next")); go.setProperty("primary", True)
        buttons.addWidget(cancel); buttons.addWidget(go); layout.addLayout(buttons)

        def proceed() -> None:
            if dest_new.isChecked() and not dest_edit.text().strip():
                QMessageBox.warning(dialog, tr("restore_wizard.title"), tr("restore_selected.destination_missing")); return
            dialog.accept()
        go.clicked.connect(proceed)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            replace, selected, destination = dest_replace.isChecked(), scope_selected.isChecked(), dest_edit.text().strip()
        finally:
            dialog.deleteLater()
        if replace:
            self.replace_original_file(snapshot_id)
            return
        if selected:
            self.restore_selected_files(snapshot_id, destination, True)
            return
        if not self.confirm_restore_summary19(snapshot_id, "all", destination):
            return
        try:
            self.prepare_restore_folder(destination, snapshot_id)
            self.controller.queue_restore_to_directory(snapshot_id, destination)
            self.remember_restore_folder(destination)
            self.show_progress(tr("progress.restore"))
        except AppError as exc:
            QMessageBox.warning(self, tr("dialog.cannot_restore"), self.controller.i18n.error_message(exc))

    def show_events19(self) -> None:
        """Recent user-visible operations with plain results; background queries are hidden."""
        tr = self.controller.i18n.tr
        hidden = {OperationType.RUNTIME_TICK.value, OperationType.REFRESH_HEALTH.value,
                  OperationType.LIST_SNAPSHOT_FILES.value, OperationType.CHECK_DATABASE.value}
        names = {s.id: s.display_name for s in (self.controller.repo.plan_summaries(False)
                                                 + self.controller.repo.plan_summaries(True))}
        with self.controller.repo.db.read_connection() as conn:
            rows = conn.execute(
                "SELECT type,state,plan_id,created_at_utc_ms,error_code FROM Operations "
                "ORDER BY created_at_utc_ms DESC, rowid DESC LIMIT 300").fetchall()
        rows = [row for row in rows if row[0] not in hidden][:150]
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("events.title")); dialog.resize(920, 520)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(len(rows), 5)
        table.setHorizontalHeaderLabels([tr("events.time"), tr("events.set"), tr("events.op"),
                                         tr("events.result"), tr("events.code")])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        def label(prefix: str, value: str) -> str:
            key = prefix + str(value)
            return tr(key) if self.controller.i18n.has_key(key) else str(value)
        for index, (kind, state, plan_id, created, code) in enumerate(rows):
            values = (fmt_time(created), names.get(plan_id, "—"), label("opkind.", kind),
                      label("opstate.", state), code or "")
            for column, value in enumerate(values):
                table.setItem(index, column, QTableWidgetItem(value))
        layout.addWidget(table, 1)
        if not rows:
            layout.addWidget(QLabel(tr("events.empty")))
        buttons = QHBoxLayout()
        problems = QPushButton(tr("events.problems"))
        problems.clicked.connect(lambda: (dialog.accept(), self.open_health_filter("all")))
        close = QPushButton(tr("common.close")); close.clicked.connect(dialog.accept)
        buttons.addWidget(problems); buttons.addStretch(); buttons.addWidget(close)
        layout.addLayout(buttons)
        dialog.exec()

    def show_health_center(self) -> None:
        self.open_health_filter("all")

    def show_help(self, page: int = 0) -> None:
        try:
            self.controller.repo.set_setting(SETTING_HELP_SHOWN, True)
        except AppError:
            logging.exception("Unable to save help-shown flag")
        dialog = HelpDialog(self.controller, self, page if isinstance(page, int) else 0)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        # Also covers the first manual opening after an autostart to the tray.
        timer = getattr(self, "_first_support_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self.maybe_show_first_support)
            self._first_support_timer = timer
        timer.start(450)

    def maybe_show_first_support(self) -> bool:
        """Show Support once per preserved data profile, only on an installer first-run request."""
        if getattr(self, "_support_first_launch_busy", False):
            return True
        if (
            not self.isVisible()
            or self.controller._shutdown_requested
            or self.controller.active_operation_id
        ):
            return False
        try:
            repo = self.controller.repo
            if repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_SHOWN, False):
                return False
            if not repo.get_bool_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, False):
                return False
            # Do not open over another dialog (for example the first-run help).
            if QApplication.activeModalWidget() is not None:
                return False
            self._support_first_launch_busy = True
            self.show_support()
            repo.set_setting(SETTING_SUPPORT_FIRST_RUN_SHOWN, True)
            repo.set_setting(SETTING_SUPPORT_FIRST_RUN_PENDING, False)
            self._support_was_shown_this_launch = True
            return True
        except Exception:
            logging.exception("Unable to display first-install Support")
            return False
        finally:
            self._support_first_launch_busy = False

    def maybe_show_first_launch(self) -> None:
        if getattr(self, "_support_was_shown_this_launch", False):
            return
        if self.maybe_show_first_support():
            return
        self.maybe_show_welcome_help()

    def maybe_show_welcome_help(self) -> None:
        try:
            if self.controller.repo.get_bool_setting(SETTING_HELP_SHOWN, False):
                return
        except AppError:
            return
        if self.isVisible():
            self.show_help()

    def show_support(self) -> None:
        from amvibackup_support import SupportDialog

        dialog = SupportDialog(self.controller.i18n, self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def show_settings(self) -> None:
        dialog = SettingsDialog(
            self.controller,
            self.paths,
            self,
        )
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def cancel_active(self) -> None:
        self.controller.cancel_active()

    def _shutdown_ready(self) -> None:
        if self._shutdown_close_scheduled:
            return

        self._shutdown_close_scheduled = True
        self._explicit_exit_requested = True
        # The timer belongs to the window: when the window is destroyed the deferred close is cancelled by itself.
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self.close)
        self._shutdown_close_timer = timer
        timer.start(0)

    def closeEvent(self, event: Any) -> None:
        if self.controller._shutdown_completed:
            self._cancel_ui_refresh()
            if self.tray_runtime is not None:
                self.tray_runtime.stop()
            event.accept()
            if not self.isVisible():
                # A window hidden in the tray does not trigger "last window closed",
                # so the event loop must be stopped explicitly (otherwise the process lingers).
                self._schedule_application_quit()
            return

        if self.controller._shutdown_requested:
            event.ignore()
            return

        if (
            not self._explicit_exit_requested
            and self.tray_runtime is not None
            and self.tray_runtime.tray is not None
            and self.close_to_tray_enabled()
        ):
            event.ignore()
            self.tray_runtime.hide_window()
            return

        tr = self.controller.i18n.tr

        if self.controller.active_operation_id:
            answer = QMessageBox.question(
                self,
                tr("dialog.active_operation_title"),
                tr("dialog.active_operation_question"),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                self._explicit_exit_requested = False
                return

        event.ignore()
        self._explicit_exit_requested = True

        central = self.centralWidget()
        if central is not None:
            central.setEnabled(False)

        if (
            self.tray_runtime is not None
            and self.tray_runtime.exit_action is not None
        ):
            self.tray_runtime.exit_action.setEnabled(False)

        self.show_progress(
            tr("progress.shutdown"),
            "",
            cancellable=False,
        )
        self._cancel_ui_refresh()
        self.controller.begin_shutdown()



class AppController(QObject):
    request_execute = Signal(object)
    operation_progress = Signal(object)
    operation_finished = Signal(object)
    shutdown_finished = Signal()
    background_query_finished = Signal(object)
    health_refresh_finished = Signal(object)
    notification_requested = Signal(object)

    def __init__(
        self,
        paths: AppPaths,
        start_worker: bool = True,
    ) -> None:
        super().__init__()

        self.paths = paths
        self.db = Database(paths.db_path)
        self.db.migrate()
        self.db.verify_contract()

        self.repo = Repository(self.db)
        self.i18n = I18nService(self.repo)
        self.storage = StorageService()
        self.health_service = HealthService(self.repo)

        self.snapshot_service = SnapshotService(
            self.repo,
            self.storage,
            self.health_service,
        )
        self.verifier = VerificationService(
            self.repo,
            self.storage,
            self.health_service,
        )
        self.restore_service = RestoreService(
            self.repo,
            self.verifier,
            self.health_service,
        )
        self.restore_test_service = RestoreTestService(self.repo, self.restore_service)
        self.delete_service = DeleteService(
            self.repo,
            self.storage,
            self.health_service,
        )
        self.retention_service = RetentionService(
            self.repo,
            self.storage,
            self.health_service,
        )

        self.auto_coordinator = AutoBackupCoordinator(
            self.repo,
            self.health_service,
            self,
        )
        self.polling_watcher = PollingChangeWatcher(
            self.repo,
            self.health_service,
            self.auto_coordinator,
        )
        self.schedule_coordinator = ScheduleCoordinator(
            self.repo,
            self.auto_coordinator,
        )
        self.autostart_service = WindowsAutostartService()
        self.notification_service = NotificationService(
            self.repo,
            self._tray_icon_for_notifications,
        )

        self.automation_runtime_timer: QTimer | None = None
        self._automation_last_watcher_poll_utc_ms: int | None = None
        self._automation_last_schedule_tick_utc_ms: int | None = None
        self._automation_runtime_running = False
        self._retention_clock_lock = threading.Lock()
        self._retention_clock_wall_ms = utc_ms()
        self._retention_clock_monotonic = time.monotonic()
        self._retention_clock_last_checkpoint = self._retention_clock_monotonic
        self._initialize_retention_clock_guard()

        self._runtime_tick_pending = False
        self._runtime_tick_draining = False
        self._runtime_generation = 0

        self.active_operation_id: str | None = None
        self._shutdown_requested = False
        self._shutdown_completed = False
        self._shutdown_timer: QTimer | None = None
        self._shutdown_quit_sent = False
        self._last_finished_operation_id: str | None = None

        self._background_queries: dict[
            str, tuple[str, str | None]
        ] = {}
        self._snapshot_files_cache: tuple[
            str, str, tuple[str, ...]
        ] | None = None

        # The health cache always consists of two linked structures:
        # the displayed state and the metadata of that state.
        # The UI can safely use the cache before the first worker reply.
        self._health_by_plan_id: dict[
            str, PlanHealth
        ] = {}

        self._health_cache_meta: dict[
            str, dict[str, Any]
        ] = {}

        self._health_refresh_pending = False
        self._health_refresh_draining = False
        self._health_refresh_generation = 0
        self._health_drain_timer = QTimer(self)
        self._health_drain_timer.setSingleShot(True)
        self._health_drain_timer.setInterval(0)
        self._health_drain_timer.timeout.connect(self._drain_health_refresh)

        self.main_window: MainWindow | None = None
        self.thread: QThread | None = None
        self.worker: OperationWorker | None = None

        self.recovery_service = RecoveryService(
            self.repo,
            self.health_service,
        )
        self.recovery_service.run()

        self.notification_requested.connect(
            self._deliver_notification
        )

        if start_worker:
            self.thread = QThread()
            self.worker = OperationWorker(self)
            self.worker.moveToThread(self.thread)

            self.request_execute.connect(self.worker.execute)
            self.worker.progress.connect(self.operation_progress)
            self.worker.finished.connect(self._finished)
            self.thread.finished.connect(self.worker.deleteLater)

            self.thread.start()

    @staticmethod
    def _retention_timezone_signature() -> str:
        return json_text({
            "tzname": list(time.tzname),
            "timezone": int(time.timezone),
            "altzone": int(getattr(time, "altzone", time.timezone)),
            "daylight": int(time.daylight),
        })

    def _initialize_retention_clock_guard(self) -> None:
        now = utc_ms()
        previous = self.repo.get_setting(SETTING_RETENTION_CLOCK_WALL_UTC_MS, None)
        previous_zone = self.repo.get_setting(SETTING_RETENTION_CLOCK_TIMEZONE, None)
        zone = self._retention_timezone_signature()
        blocked = bool(self.repo.get_setting(SETTING_RETENTION_CLOCK_BLOCKED, False))
        if isinstance(previous, int):
            if now + RETENTION_CLOCK_JUMP_SECONDS * 1000 < previous:
                blocked = True
            if now - previous > RETENTION_CLOCK_LONG_GAP_DAYS * 86400000:
                blocked = True
        if isinstance(previous_zone, str) and previous_zone != zone:
            blocked = True
        self.repo.set_settings({
            SETTING_RETENTION_CLOCK_BLOCKED: blocked,
            SETTING_RETENTION_CLOCK_WALL_UTC_MS: now,
            SETTING_RETENTION_CLOCK_TIMEZONE: zone,
        })

    def _observe_retention_clock(self, now_utc_ms: int) -> None:
        monotonic_now = time.monotonic()
        with self._retention_clock_lock:
            expected = self._retention_clock_wall_ms + int(
                (monotonic_now - self._retention_clock_monotonic) * 1000
            )
            jumped = abs(int(now_utc_ms) - expected) > RETENTION_CLOCK_JUMP_SECONDS * 1000
            zone_changed = self.repo.get_setting(
                SETTING_RETENTION_CLOCK_TIMEZONE, self._retention_timezone_signature()
            ) != self._retention_timezone_signature()
            checkpoint = monotonic_now - self._retention_clock_last_checkpoint >= RETENTION_CLOCK_CHECKPOINT_SECONDS
            if jumped or zone_changed or checkpoint:
                values = {
                    SETTING_RETENTION_CLOCK_WALL_UTC_MS: int(now_utc_ms),
                    SETTING_RETENTION_CLOCK_TIMEZONE: self._retention_timezone_signature(),
                }
                if jumped or zone_changed:
                    values[SETTING_RETENTION_CLOCK_BLOCKED] = True
                self.repo.set_settings(values)
                self._retention_clock_last_checkpoint = monotonic_now
            self._retention_clock_wall_ms = int(now_utc_ms)
            self._retention_clock_monotonic = monotonic_now

    def start_automation_runtime(
        self,
        runtime_tick_seconds: int = DEFAULT_AUTOMATION_RUNTIME_TICK_SECONDS,
    ) -> None:
        if self._shutdown_requested or self._shutdown_completed:
            raise AppError(
                "APP_SHUTTING_DOWN",
                "Automation cannot be started while the application is exiting.",
            )

        if not (
            MIN_AUTOMATION_RUNTIME_TICK_SECONDS
            <= runtime_tick_seconds
            <= MAX_AUTOMATION_RUNTIME_TICK_SECONDS
        ):
            raise AppError(
                "AUTOMATION_RUNTIME_INTERVAL_INVALID",
                "The automation runtime interval must be from "
                f"{MIN_AUTOMATION_RUNTIME_TICK_SECONDS} to "
                f"{MAX_AUTOMATION_RUNTIME_TICK_SECONDS} seconds.",
            )

        if self._automation_runtime_running:
            return

        self.automation_runtime_timer = QTimer(self)
        self.automation_runtime_timer.setInterval(
            runtime_tick_seconds * 1000
        )
        self.automation_runtime_timer.timeout.connect(
            self._automation_runtime_tick
        )

        self._automation_runtime_running = True
        self._automation_runtime_tick()
        self.automation_runtime_timer.start()

    def stop_automation_runtime(self) -> None:
        self._automation_runtime_running = False
        self._runtime_tick_pending = False
        self._runtime_tick_draining = False
        self._runtime_generation += 1

        if self.automation_runtime_timer is not None:
            self.automation_runtime_timer.stop()
            self.automation_runtime_timer.deleteLater()
            self.automation_runtime_timer = None

    def automation_runtime_tick_for_test(
        self,
        now_utc_ms: int,
        now_local: datetime,
    ) -> dict[str, list[AutoDecision]]:
        """Deterministic single tick without QTimer.

        Used only by self-tests. Does not start separate threads.
        May create an actual automatic backup only through the existing controller
        queue path, if the worker is active.
        """
        return self._automation_runtime_tick_at(
            now_utc_ms,
            now_local,
        )

    def _health_metadata_signature(
        self,
        plan: PlanDetails,
    ) -> str:
        issues = self.repo.list_active_issues(plan.id)

        material = {
            "id": plan.id,
            "source": plan.source_path,
            "kind": plan.source_kind,
            "identity": plan.source_identity_json,
            "storage": plan.storage_path,
            "mode": plan.storage_mode,
            "state": plan.state,
            "last_snapshot": plan.last_success_snapshot_id,
            "count": plan.snapshot_count,
            "last_snapshot_at": (
                plan.last_snapshot_at_utc_ms
            ),
            "last_operation": plan.last_operation_state,
            "issues": sorted(
                (
                    issue.key,
                    issue.severity,
                    issue.code,
                )
                for issue in issues
            ),
            "recovery": self.repo.needs_recovery_for_plan(
                plan.id
            ),
            "deleting": (
                self.repo.plan_deletion_job(plan.id)
                is not None
            ),
        }

        return sha256_bytes(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        )

    def _valid_health_cache_meta(
        self,
        plan: PlanDetails,
    ) -> dict[str, Any] | None:
        meta = self._health_cache_meta.get(plan.id)

        if meta is None:
            return None

        if (
            meta["signature"]
            != self._health_metadata_signature(plan)
        ):
            return None

        if utc_ms() - meta["checked_at"] > 60000:
            return None

        return meta

    def cached_plan_health(
        self,
        plan_id: str,
    ) -> PlanHealth:
        plan = self.repo.plan_details(plan_id)

        cached = self._health_by_plan_id.get(plan_id)
        if (
            cached is not None
            and self._valid_health_cache_meta(plan)
            is not None
        ):
            return cached

        # Safe metadata projection.
        # There is deliberately no probe_source(), probe_storage(),
        # evaluate() or other file checks here.
        issues = self.repo.list_active_issues(plan_id)

        if self.repo.plan_deletion_job(plan_id):
            state = HealthState.ERROR.value

        elif plan.state == PlanState.ARCHIVED.value:
            state = HealthState.ARCHIVED.value

        elif plan.state == PlanState.PAUSED.value:
            state = HealthState.PAUSED.value

        elif self.repo.unfinished_operations_for_plan(plan_id):
            state = HealthState.RUNNING.value

        elif (
            not plan.source_identity_json
            or self.repo.needs_recovery_for_plan(plan_id)
            or any(
                issue.severity in {
                    IssueSeverity.ERROR.value,
                    IssueSeverity.CRITICAL.value,
                }
                for issue in issues
            )
            or any(
                snapshot.state == "corrupted"
                for snapshot in self.repo.snapshots(plan_id)
            )
        ):
            state = HealthState.ERROR.value

        elif plan.snapshot_count == 0:
            state = HealthState.UNPROTECTED.value

        else:
            # Until the worker confirms the source/storage state,
            # we do not claim to the user that protection is definitely
            # active. After the worker refresh the state is replaced
            # with the actual result.
            state = HealthState.ATTENTION.value

        return PlanHealth(
            plan_id=plan_id,
            state=state,
            title=self.i18n.tr(
                f"health.state.{state}"
            ),
            detail=self.i18n.health_detail(state),
            source_available=None,
            storage_available=None,
            committed_snapshot_count=plan.snapshot_count,
            last_snapshot_at_utc_ms=(
                plan.last_snapshot_at_utc_ms
            ),
            last_operation_state=plan.last_operation_state,
            active_issue_count=len(issues),
        )

    def source_confirmation_required(
        self,
        plan_id: str,
    ) -> bool:
        plan = self.repo.plan_details(plan_id)

        if not plan.source_identity_json:
            return True

        meta = self._valid_health_cache_meta(plan)

        if (
            meta is not None
            and meta["source_error_code"]
            in {
                "SOURCE_IDENTITY_UNCONFIRMED",
                "SOURCE_IDENTITY_MISMATCH",
            }
        ):
            return True

        return any(
            issue.code in {
                "SOURCE_IDENTITY_UNCONFIRMED",
                "SOURCE_IDENTITY_MISMATCH",
            }
            for issue in self.repo.list_active_issues(plan_id)
        )

    def cached_storage_free_bytes(
        self,
        plan_id: str,
    ) -> int | None:
        plan = self.repo.plan_details(plan_id)
        meta = self._valid_health_cache_meta(plan)

        if meta is None:
            return None

        return meta["storage_free_bytes"]

    def queue_health_refresh(self) -> str | None:
        if (
            self._shutdown_requested
            or self._shutdown_completed
            or self.worker is None
        ):
            return None

        for operation_id, query in self._background_queries.items():
            if (
                query[0] == OperationType.REFRESH_HEALTH.value
                and operation_id == self.active_operation_id
            ):
                self._health_refresh_pending = True
                return operation_id

        if self.active_operation_id is not None:
            self._health_refresh_pending = True
            return None

        self._health_refresh_pending = False
        return self._queue(OperationType.REFRESH_HEALTH)

    def _perform_health_refresh(
        self,
        operation_id: str,
        cancelled: Callable[[], bool],
    ) -> OperationResult:
        if cancelled():
            raise CancelledError()

        plans = (
            self.repo.plan_summaries(False)
            + self.repo.plan_summaries(True)
        )
        payload: list[dict[str, Any]] = []

        for summary in plans:
            if cancelled():
                raise CancelledError()

            plan = self.repo.plan_details(summary.id)
            item, source, storage = (
                self.health_service.evaluate_with_probes(plan)
            )
            if cancelled():
                raise CancelledError()

            # evaluate updates issues; sign the resulting metadata.
            # The probes and displayed health belong to one evaluation.
            signature = self._health_metadata_signature(plan)
            payload.append(
                {
                    "plan_id": item.plan_id,
                    "state": item.state,
                    "title": item.title,
                    "detail": item.detail,
                    "source_available": item.source_available,
                    "storage_available": item.storage_available,
                    "committed_snapshot_count": item.committed_snapshot_count,
                    "last_snapshot_at_utc_ms": item.last_snapshot_at_utc_ms,
                    "last_operation_state": item.last_operation_state,
                    "active_issue_count": item.active_issue_count,
                    "signature": signature,
                    "checked_at": utc_ms(),
                    "source_error_code": source.error_code,
                    "storage_free_bytes": storage.free_bytes,
                }
            )

        if cancelled():
            raise CancelledError()

        return OperationResult(
            operation_id,
            True,
            "The plan status check finished.",
            result_code="HEALTH_REFRESH_SUCCEEDED",
            result_data={"plans": payload},
        )

    def _accept_health_refresh(
        self,
        result: OperationResult,
    ) -> None:
        if (
            self._shutdown_requested
            or self._shutdown_completed
            or not result.success
        ):
            return

        data = result.result_data

        if (
            not isinstance(data, dict)
            or not isinstance(data.get("plans"), list)
        ):
            result.success = False
            result.error_code = (
                "BACKGROUND_QUERY_RESULT_MISSING"
            )
            result.message = (
                "A valid plan check result "
                "was not received."
            )
            return

        parsed: dict[str, PlanHealth] = {}
        metadata: dict[str, dict[str, Any]] = {}

        for item in data["plans"]:
            if not isinstance(item, dict):
                result.success = False
                result.error_code = (
                    "BACKGROUND_QUERY_RESULT_MISSING"
                )
                result.message = (
                    "The plan check result has "
                    "an invalid format."
                )
                return

            required = {
                "plan_id",
                "state",
                "title",
                "detail",
                "source_available",
                "storage_available",
                "committed_snapshot_count",
                "last_snapshot_at_utc_ms",
                "last_operation_state",
                "active_issue_count",
                "signature",
                "checked_at",
                "source_error_code",
                "storage_free_bytes",
            }

            if not required.issubset(item):
                result.success = False
                result.error_code = (
                    "BACKGROUND_QUERY_RESULT_MISSING"
                )
                result.message = (
                    "The plan check result is incomplete."
                )
                return

            if (
                not isinstance(item["plan_id"], str)
                or not isinstance(item["state"], str)
                or not isinstance(item["title"], str)
                or not isinstance(item["detail"], str)
                or not isinstance(item["signature"], str)
                or not isinstance(item["checked_at"], int)
                or not isinstance(
                    item["committed_snapshot_count"],
                    int,
                )
                or not isinstance(
                    item["active_issue_count"],
                    int,
                )
            ):
                result.success = False
                result.error_code = (
                    "BACKGROUND_QUERY_RESULT_MISSING"
                )
                result.message = (
                    "The plan check result contains "
                    "invalid fields."
                )
                return

            if (
                item["state"]
                not in {
                    state.value
                    for state in HealthState
                }
            ):
                result.success = False
                result.error_code = (
                    "BACKGROUND_QUERY_RESULT_MISSING"
                )
                result.message = (
                    "The check result contains "
                    "an unknown state."
                )
                return

            parsed[item["plan_id"]] = PlanHealth(
                plan_id=item["plan_id"],
                state=item["state"],
                title=item["title"],
                detail=item["detail"],
                source_available=item["source_available"],
                storage_available=item["storage_available"],
                committed_snapshot_count=(
                    item["committed_snapshot_count"]
                ),
                last_snapshot_at_utc_ms=(
                    item["last_snapshot_at_utc_ms"]
                ),
                last_operation_state=(
                    item["last_operation_state"]
                ),
                active_issue_count=item["active_issue_count"],
            )

            metadata[item["plan_id"]] = {
                "signature": item["signature"],
                "checked_at": item["checked_at"],
                "source_error_code": item[
                    "source_error_code"
                ],
                "storage_free_bytes": item[
                    "storage_free_bytes"
                ],
            }

        self._health_by_plan_id = parsed
        self._health_cache_meta = metadata
        self._health_refresh_generation += 1

    def _schedule_health_refresh_drain(self) -> None:
        if (
            not self._health_refresh_pending
            or self._health_refresh_draining
            or self._shutdown_requested
            or self._shutdown_completed
        ):
            return
        self._health_refresh_draining = True
        self._health_drain_timer.start()


    def _cancel_health_refresh_drain(self) -> None:
        self._health_drain_timer.stop()
        self._health_refresh_pending = False
        self._health_refresh_draining = False

    def _drain_health_refresh(self) -> None:
        self._health_refresh_draining = False
        if self._shutdown_requested or self._shutdown_completed:
            self._cancel_health_refresh_drain()
            return
        if self._health_refresh_pending:
            self.queue_health_refresh()


    def request_runtime_tick(self) -> str | None:
        if (
            self._shutdown_requested
            or self._shutdown_completed
            or not self._automation_runtime_running
        ):
            return None

        if self.worker is None:
            return None

        for operation_id, query in self._background_queries.items():
            if (
                query[0] == OperationType.RUNTIME_TICK.value
                and operation_id == self.active_operation_id
            ):
                self._runtime_tick_pending = True
                return operation_id

        if self.active_operation_id is not None:
            self._runtime_tick_pending = True
            return None

        self._runtime_tick_pending = False

        return self._queue(
            OperationType.RUNTIME_TICK,
            now_utc_ms=utc_ms(),
            now_local_iso=datetime.now().isoformat(),
        )

    def _perform_runtime_tick(
        self,
        operation_id: str,
        now_utc_ms: int,
        now_local_iso: str,
        cancelled: Callable[[], bool],
    ) -> OperationResult:
        if cancelled():
            raise CancelledError()

        now_local = datetime.fromisoformat(now_local_iso)
        self._observe_retention_clock(now_utc_ms)
        watcher = []
        schedule = []

        previous = self._automation_last_watcher_poll_utc_ms

        if (
            previous is None
            or now_utc_ms - previous
            >= DEFAULT_WATCHER_POLL_SECONDS * 1000
        ):
            watcher = self.polling_watcher.poll_once(now_utc_ms)
            self._automation_last_watcher_poll_utc_ms = now_utc_ms

        if cancelled():
            raise CancelledError()

        previous = self._automation_last_schedule_tick_utc_ms

        if (
            previous is None
            or now_utc_ms - previous
            >= DEFAULT_SCHEDULE_TICK_SECONDS * 1000
        ):
            schedule = self.schedule_coordinator.tick(now_utc_ms)
            self._automation_last_schedule_tick_utc_ms = now_utc_ms

        if cancelled():
            raise CancelledError()

        decisions = self.auto_coordinator.evaluate_pending(
            now_utc_ms,
            now_local,
            None,
        )

        if cancelled():
            raise CancelledError()

        return OperationResult(
            operation_id,
            True,
            "The background automation tick finished.",
            result_code="RUNTIME_TICK_SUCCEEDED",
            result_data={
                "ready_plan_ids": [
                    decision.plan_id
                    for decision in decisions
                    if decision.action == "queued"
                ],
                "watcher_count": len(watcher),
                "schedule_count": len(schedule),
            },
        )

    def _accept_runtime_tick(
        self,
        result: OperationResult,
    ) -> None:
        if (
            self._shutdown_requested
            or self._shutdown_completed
            or not self._automation_runtime_running
            or not result.success
        ):
            return

        data = result.result_data or {}
        ready = data.get("ready_plan_ids", [])

        if not isinstance(ready, list):
            return

        now_utc_ms = utc_ms()
        now_local = datetime.now()

        for plan_id in ready:
            if (
                not isinstance(plan_id, str)
                or self.active_operation_id is not None
            ):
                break

            try:
                plan = self.repo.plan_details(plan_id)

                if (
                    plan.state != PlanState.ENABLED.value
                    or not plan.pending_trigger_mask
                    or self.repo.plan_deletion_job(plan_id)
                    or self.repo.needs_recovery_for_plan(plan_id)
                ):
                    continue

                policy = self.repo.get_trigger_policy(plan_id)

                if (
                    policy.quiet_hours_enabled
                    and is_in_quiet_hours(
                        now_local,
                        policy.quiet_hours_start_local,
                        policy.quiet_hours_end_local,
                    )
                ):
                    continue

                if (
                    plan.last_auto_backup_at_utc_ms is not None
                    and now_utc_ms - plan.last_auto_backup_at_utc_ms
                    < policy.min_auto_interval_minutes * 60000
                ):
                    continue

                if plan.pending_trigger_mask & TRIGGER_MASK_EVENT:
                    if not policy.on_change_enabled:
                        continue

                    if (
                        plan.pending_trigger_since_utc_ms is None
                        or now_utc_ms
                        - plan.pending_trigger_since_utc_ms
                        < policy.change_debounce_seconds * 1000
                    ):
                        continue

                    reason = TriggerReason.EVENT

                elif (
                    plan.pending_trigger_mask
                    & TRIGGER_MASK_SCHEDULE
                ):
                    if not policy.on_schedule_enabled:
                        continue

                    reason = TriggerReason.SCHEDULE

                else:
                    if not policy.on_schedule_enabled:
                        continue

                    reason = TriggerReason.STARTUP_CATCHUP

                op = self.queue_backup(
                    plan_id,
                    False,
                    reason,
                )
                self.repo.activity(
                    "info",
                    "AUTO_BACKUP_STARTED",
                    "The automatic backup was queued.",
                    plan_id,
                    op,
                    {
                        "trigger_reason": reason.value,
                        "trigger_mask": plan.pending_trigger_mask,
                    },
                )

                # The other plans keep their pending state.
                # Only one backup runs at a time.
                break

            except AppError as exc:
                logging.info(
                    "Automatic backup was not queued for %s: %s",
                    plan_id,
                    exc.code,
                )

    def _schedule_runtime_drain(self) -> None:
        if (
            not self._runtime_tick_pending
            or self._runtime_tick_draining
            or self._shutdown_requested
            or not self._automation_runtime_running
        ):
            return

        self._runtime_tick_draining = True
        generation = self._runtime_generation

        QTimer.singleShot(
            0,
            lambda: self._drain_runtime_tick(generation),
        )

    def _drain_runtime_tick(self, generation: int) -> None:
        if generation != self._runtime_generation:
            return

        self._runtime_tick_draining = False

        if self._runtime_tick_pending:
            self.request_runtime_tick()

    @Slot(object)
    def _deliver_notification(self, packet: tuple) -> None:
        if self._shutdown_requested:
            return

        self.notify_event(
            *packet[:5],
            force=packet[5],
        )

    def _automation_runtime_tick(self) -> None:
        if (
            not self._automation_runtime_running
            or self._shutdown_requested
        ):
            return

        try:
            self.request_runtime_tick()

        except AppError as exc:
            logging.info(
                "Runtime tick deferred: %s",
                exc.code,
            )

        except Exception:
            logging.exception(
                "Automation runtime request failed"
            )

    def _automation_runtime_tick_at(
        self,
        now_utc_ms: int,
        now_local: datetime,
    ) -> dict[str, list[AutoDecision]]:
        watcher_decisions: list[AutoDecision] = []
        schedule_decisions: list[AutoDecision] = []
        process_decisions: list[AutoDecision] = []

        watcher_interval_ms = (
            DEFAULT_WATCHER_POLL_SECONDS
            * 1000
        )
        schedule_interval_ms = (
            DEFAULT_SCHEDULE_TICK_SECONDS
            * 1000
        )

        should_poll_watcher = (
            self._automation_last_watcher_poll_utc_ms is None
            or now_utc_ms
            - self._automation_last_watcher_poll_utc_ms
            >= watcher_interval_ms
        )

        if should_poll_watcher:
            watcher_decisions = self.polling_watcher.poll_once(
                now_utc_ms
            )
            self._automation_last_watcher_poll_utc_ms = now_utc_ms

        should_tick_schedule = (
            self._automation_last_schedule_tick_utc_ms is None
            or now_utc_ms
            - self._automation_last_schedule_tick_utc_ms
            >= schedule_interval_ms
        )

        if should_tick_schedule:
            schedule_decisions = self.schedule_coordinator.tick(
                now_utc_ms
            )
            self._automation_last_schedule_tick_utc_ms = now_utc_ms

        if self.worker is not None:
            process_decisions = self.process_automatic_pending(
                now_utc_ms,
                now_local,
            )

        return {
            "watcher": watcher_decisions,
            "schedule": schedule_decisions,
            "process": process_decisions,
        }

    @Slot(object)
    def _finished(self, result: OperationResult) -> None:
        if (
            self.active_operation_id is not None
            and result.operation_id != self.active_operation_id
        ):
            logging.warning(
                "Ignoring stale operation result %s",
                result.operation_id,
            )
            return

        if (
            result.operation_id
            == self._last_finished_operation_id
        ):
            return

        self._last_finished_operation_id = result.operation_id
        self.active_operation_id = None
        self._active_cancel_event = None

        is_automatic = (
            result.plan_id is not None
            and result.trigger_reason in {
                TriggerReason.EVENT.value,
                TriggerReason.SCHEDULE.value,
                TriggerReason.STARTUP_CATCHUP.value,
            }
        )

        if is_automatic:
            success_or_no_changes = (
                result.success
                and result.error_code is None
            )

            try:
                self.auto_coordinator.complete_automatic_run(
                    result.plan_id,
                    utc_ms(),
                    success_or_no_changes,
                )

                if success_or_no_changes:
                    self.repo.activity(
                        "info",
                        "AUTO_BACKUP_SUCCEEDED",
                        "The automatic backup finished successfully.",
                        result.plan_id,
                        result.operation_id,
                        {
                            "trigger_reason": result.trigger_reason,
                        },
                    )
                    self.notify_event(
                        "AUTO_BACKUP_SUCCEEDED",
                        self.i18n.tr(
                            "notification.auto_success_title"
                        ),
                        self.i18n.tr(
                            "notification.auto_no_changes"
                            if result.result_code == "BACKUP_NO_CHANGES"
                            else "notification.auto_created"
                        ),
                        NotificationSeverity.INFO,
                        result.plan_id,
                    )

                else:
                    self.repo.activity(
                        "warning",
                        "AUTO_BACKUP_FAILED",
                        "The automatic backup did not finish; "
                        "the pending trigger was kept.",
                        result.plan_id,
                        result.operation_id,
                        {
                            "trigger_reason": result.trigger_reason,
                            "error_code": result.error_code,
                        },
                    )
                    self.notify_event(
                        "AUTO_BACKUP_FAILED",
                        self.i18n.tr(
                            "notification.auto_failed_title"
                        ),
                        self.i18n.operation_message(result),
                        NotificationSeverity.ERROR,
                        result.plan_id,
                    )

            except Exception:
                logging.exception(
                    "Cannot finalize automatic backup lifecycle"
                )

        query = self._background_queries.pop(
            result.operation_id,
            None,
        )

        if query is not None:
            if query[0] == OperationType.RUNTIME_TICK.value:
                self._accept_runtime_tick(result)

            if query[0] == OperationType.REFRESH_HEALTH.value:
                self._accept_health_refresh(result)

            if (
                result.success
                and query[0]
                == OperationType.LIST_SNAPSHOT_FILES.value
            ):
                data = result.result_data

                if (
                    not isinstance(data, dict)
                    or data.get("snapshot_id") != query[1]
                    or not isinstance(
                        data.get("manifest_sha256"),
                        str,
                    )
                    or not isinstance(
                        data.get("relative_paths"),
                        list,
                    )
                    or not all(
                        isinstance(path, str)
                        for path in data["relative_paths"]
                    )
                ):
                    result.success = False
                    result.error_code = (
                        "BACKGROUND_QUERY_RESULT_MISSING"
                    )
                    result.message = (
                        "A valid file list was not received. "
                        "Try loading again."
                    )
                else:
                    self._snapshot_files_cache = (
                        data["snapshot_id"],
                        data["manifest_sha256"],
                        tuple(data["relative_paths"]),
                    )

            self.background_query_finished.emit(result)
            if query[0] == OperationType.REFRESH_HEALTH.value:
                self.health_refresh_finished.emit(result)
            self._schedule_runtime_drain()
            self._schedule_health_refresh_drain()
            return

        self.operation_finished.emit(result)
        self._schedule_runtime_drain()
        self._schedule_health_refresh_drain()

    def begin_shutdown(self) -> None:
        if self._shutdown_completed or self._shutdown_requested:
            return

        self._shutdown_requested = True
        self._cancel_health_refresh_drain()
        self.stop_automation_runtime()
        self.cancel_active()
        self._shutdown_quit_sent = False

        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(50)
        self._shutdown_timer.timeout.connect(
            self._poll_shutdown
        )
        self._shutdown_timer.start()
        self._poll_shutdown()

    def _stop_shutdown_timer(self) -> None:
        timer = self._shutdown_timer
        self._shutdown_timer = None

        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _poll_shutdown(self) -> None:
        if self._shutdown_completed:
            self._stop_shutdown_timer()
            return

        if not self._shutdown_requested:
            return

        thread = self.thread

        if self.active_operation_id is not None:
            # While the worker runs, the GUI keeps processing events.
            if (
                thread is None
                or thread.isRunning()
                or not thread.wait(0)
            ):
                return

            # The thread has already stopped, but there is no operation result.
            # Such an operation cannot be declared safely cancelled.
            operation_id = self.active_operation_id
            row = self.repo.operation(operation_id)

            if row["state"] in {"queued", "running"}:
                self.repo.recovery_mark(operation_id)

                if row["plan_id"]:
                    self.health_service.record_error(
                        AppError(
                            "OPERATION_NEEDS_RECOVERY",
                            "The worker finished without a result; "
                            "check the operation.",
                        ),
                        row["plan_id"],
                        row["snapshot_id"],
                    )

                row = self.repo.operation(operation_id)

            payload = self.repo.operation_payload(operation_id)
            succeeded = row["state"] == "succeeded"

            self._finished(
                OperationResult(
                    operation_id,
                    succeeded,
                    "The operation outcome was checked against the log.",
                    (
                        None
                        if succeeded
                        else (
                            row["error_code"]
                            or "OPERATION_NEEDS_RECOVERY"
                        )
                    ),
                    trigger_reason=payload.get("trigger_reason"),
                    plan_id=row["plan_id"],
                )
            )

        if thread is not None:
            if not self._shutdown_quit_sent:
                self._shutdown_quit_sent = True
                thread.quit()

            # Non-blocking check that the thread has really finished.
            if not thread.wait(0):
                return

            thread.deleteLater()
            self.thread = None
            self.worker = None

        self._stop_shutdown_timer()

        try:
            self.db.checkpoint_wal()
        except Exception:
            # The WAL is not deleted: already committed data is kept.
            logging.exception(
                "Final WAL checkpoint failed; committed WAL is retained"
            )

        self._shutdown_completed = True
        self.shutdown_finished.emit()

    def shutdown(self) -> bool:
        # Synchronous fallback path for tests and cleanup after the GUI.
        # A normal MainWindow close calls begin_shutdown().
        if self._shutdown_completed:
            return True

        self._shutdown_requested = True
        self._cancel_health_refresh_drain()
        self.stop_automation_runtime()
        self._stop_shutdown_timer()
        self.cancel_active()

        if self.thread is not None:
            self.thread.quit()

            if not self.thread.wait(30000):
                logging.error(
                    "Worker thread did not finish "
                    "during fallback shutdown"
                )
                return False

            self.thread.deleteLater()
            self.thread = None
            self.worker = None

        self.db.checkpoint_wal()
        self._shutdown_completed = True
        return True

    def create_or_update_plan(
        self,
        plan_id: str | None,
        name: str,
        note: str,
        source_text: str,
        storage_text: str,
        keep: int,
        storage_mode: str,
    ) -> str:
        name = name.strip()

        if not name:
            raise AppError("PLAN_INVALID", "Enter a plan name.")

        if not MIN_KEEP_LAST_N <= keep <= MAX_KEEP_LAST_N:
            raise AppError(
                "PLAN_INVALID",
                "The number of versions must be from 1 to 1000.",
            )

        if storage_mode not in {
            StorageMode.DIRECT.value,
            StorageMode.ZIP.value,
        }:
            raise AppError(
                "STORAGE_MODE_INVALID",
                "Choose a supported storage mode.",
            )

        source = PathPolicy.validate_source(source_text)
        storage = PathPolicy.validate_storage(storage_text)
        PathPolicy.validate_topology(source, storage)

        storage_id = self.repo.get_or_create_storage(storage)
        self.storage.ensure_root(storage, storage_id)

        if plan_id:
            self.repo.update_plan(
                plan_id,
                name,
                note.strip(),
                source,
                storage_id,
                keep,
                storage_mode,
            )
            return plan_id

        return self.repo.create_plan(
            name,
            note.strip(),
            source,
            storage_id,
            keep,
            storage_mode,
        )

    def update_trigger_policy(
        self,
        plan_id: str,
        policy: TriggerPolicy,
    ) -> TriggerPolicy:
        return self.repo.update_trigger_policy(
            plan_id,
            policy,
        )

    def _tray_icon_for_notifications(
        self,
    ) -> QSystemTrayIcon | None:
        if self.main_window is None:
            return None

        tray_runtime = self.main_window.tray_runtime

        if tray_runtime is None:
            return None

        return tray_runtime.tray

    def notify_event(
        self,
        code: str,
        title: str,
        message: str,
        severity: NotificationSeverity,
        plan_id: str | None = None,
        force: bool = False,
    ) -> bool:
        if self._shutdown_requested:
            return False

        if (
            self.thread is not None
            and QThread.currentThread() == self.thread
        ):
            self.notification_requested.emit(
                (
                    code,
                    title,
                    message,
                    severity,
                    plan_id,
                    bool(force),
                )
            )
            # The notification is queued for delivery
            # but not shown yet.
            return False

        try:
            return self.notification_service.notify(
                code,
                title,
                message,
                severity,
                plan_id,
                force=force,
            )

        except AppError:
            logging.exception(
                "Unable to process notification policy"
            )
            return False

    def is_autostart_supported(self) -> bool:
        return self.autostart_service.is_supported()

    def is_autostart_enabled(self) -> bool:
        if not self.autostart_service.is_supported():
            return False

        return self.autostart_service.is_enabled()

    def set_autostart_enabled(
        self,
        enabled: bool,
    ) -> None:
        if enabled:
            self.autostart_service.enable()
        else:
            self.autostart_service.disable()

        self.repo.set_setting(
            SETTING_AUTOSTART_ENABLED,
            bool(enabled),
        )

    def save_runtime_settings(
        self,
        close_to_tray: bool,
        start_minimized: bool,
        autostart_enabled: bool | None,
        notifications_enabled: bool,
        success_notifications_enabled: bool,
        notification_cooldown_minutes: int,
    ) -> None:
        if not (
            MIN_NOTIFICATION_COOLDOWN_MINUTES
            <= notification_cooldown_minutes
            <= MAX_NOTIFICATION_COOLDOWN_MINUTES
        ):
            raise AppError(
                "NOTIFICATION_SETTING_INVALID",
                "The repeat notification interval must be from "
                f"{MIN_NOTIFICATION_COOLDOWN_MINUTES} to "
                f"{MAX_NOTIFICATION_COOLDOWN_MINUTES} minutes.",
            )

        previous_autostart: bool | None = None

        if autostart_enabled is not None:
            previous_autostart = self.is_autostart_enabled()

            if previous_autostart != autostart_enabled:
                if autostart_enabled:
                    self.autostart_service.enable()
                else:
                    self.autostart_service.disable()

        try:
            self.repo.set_settings(
                {
                    SETTING_CLOSE_TO_TRAY: bool(close_to_tray),
                    SETTING_START_MINIMIZED: bool(
                        start_minimized if close_to_tray else False
                    ),
                    SETTING_NOTIFICATIONS_ENABLED: bool(
                        notifications_enabled
                    ),
                    SETTING_NOTIFICATIONS_SUCCESS_ENABLED: bool(
                        success_notifications_enabled
                        if notifications_enabled
                        else False
                    ),
                    SETTING_NOTIFICATION_COOLDOWN_MINUTES: int(
                        notification_cooldown_minutes
                    ),
                    **(
                        {
                            SETTING_AUTOSTART_ENABLED: bool(
                                autostart_enabled
                            )
                        }
                        if autostart_enabled is not None
                        else {}
                    ),
                }
            )
        except Exception:
            if (
                autostart_enabled is not None
                and previous_autostart is not None
                and previous_autostart != autostart_enabled
            ):
                try:
                    if previous_autostart:
                        self.autostart_service.enable()
                    else:
                        self.autostart_service.disable()
                except Exception:
                    logging.exception(
                        "Unable to roll back Windows autostart "
                        "after settings save failure"
                    )
            raise

    def save_plan_with_policy(
        self,
        plan_id: str | None,
        name: str,
        note: str,
        source_text: str,
        storage_text: str,
        keep: int,
        storage_mode: str,
        policy: TriggerPolicy,
        retention_policy: RetentionPolicy | None = None,
        retention_preview_token: str | None = None,
    ) -> str:
        normalized_name = name.strip()
        if not normalized_name: raise AppError("PLAN_INVALID","Enter a plan name.")
        if storage_mode not in {StorageMode.DIRECT.value,StorageMode.ZIP.value}:
            raise AppError("STORAGE_MODE_INVALID","Choose a supported storage mode.")
        normalized_retention = self.repo.validate_retention_policy(
            retention_policy or RetentionPolicy(RETENTION_MODE_COUNT,keep,0,0,0))
        if retention_preview_token and self.active_operation_id:
            raise AppError(
                "OPERATION_ALREADY_RUNNING",
                "Wait for the current operation to finish before applying cleanup.",
            )
        source=PathPolicy.validate_source(source_text)
        storage=PathPolicy.validate_storage(storage_text)
        PathPolicy.validate_topology(source,storage)
        normalized_trigger=self.repo.validate_trigger_policy(policy)
        storage_id=self.repo.get_or_create_storage(storage)
        self.storage.ensure_root(storage,storage_id)
        saved=self.repo.save_plan_with_policy(
            plan_id,normalized_name,note.strip(),source,storage_id,normalized_retention.keep_last_n,
            storage_mode,normalized_trigger,normalized_retention,retention_preview_token)
        if normalized_retention.mode == RETENTION_MODE_TIME and retention_preview_token:
            self.acknowledge_retention_clock(saved)
        return saved

    def retention_change_preview(self, plan_id: str, policy: RetentionPolicy) -> RetentionPreview:
        return self.retention_service.preview(plan_id, self.repo.validate_retention_policy(policy))

    def queue_apply_retention(self, plan_id: str, preview_token: str) -> str:
        preview = self.retention_service.preview(plan_id)
        if preview.token != preview_token:
            raise AppError(
                "RETENTION_PREVIEW_STALE",
                "The history or the retention policy changed. Review the deletion list again.",
            )
        return self._queue(OperationType.APPLY_RETENTION,plan_id,preview_token=preview_token)

    def retention_clock_blocked(self) -> bool:
        return bool(self.repo.get_setting(SETTING_RETENTION_CLOCK_BLOCKED,False))

    def acknowledge_retention_clock(self, plan_id: str) -> None:
        self.repo.set_settings({
            SETTING_RETENTION_CLOCK_BLOCKED: False,
            SETTING_RETENTION_CLOCK_WALL_UTC_MS: utc_ms(),
            SETTING_RETENTION_CLOCK_TIMEZONE: self._retention_timezone_signature(),
        })
        self.repo.resolve_plan_issue(plan_id,"RETENTION_CLOCK_REVIEW_REQUIRED")

    def queue_database_check(self) -> str:
        if self._shutdown_requested or self._shutdown_completed:
            raise AppError(
                "APP_SHUTTING_DOWN",
                "The application is shutting down.",
            )

        for operation_id, query in self._background_queries.items():
            if (
                query[0] == OperationType.CHECK_DATABASE.value
                and operation_id == self.active_operation_id
            ):
                return operation_id

        return self._queue(OperationType.CHECK_DATABASE)

    def queue_snapshot_files(self, snapshot_id: str) -> str:
        if self._shutdown_requested or self._shutdown_completed:
            raise AppError(
                "APP_SHUTTING_DOWN",
                "The application is shutting down.",
            )

        for operation_id, query in self._background_queries.items():
            if (
                query
                == (
                    OperationType.LIST_SNAPSHOT_FILES.value,
                    snapshot_id,
                )
                and operation_id == self.active_operation_id
            ):
                return operation_id

        snapshot = self.repo.snapshot(snapshot_id)

        if snapshot.state != "committed":
            raise AppError(
                "SNAPSHOT_NOT_COMMITTED",
                "The version is unavailable.",
            )

        if snapshot.source_kind != SourceKind.FOLDER.value:
            raise AppError(
                "RESTORE_DESTINATION_INVALID",
                "File selection is available only for a folder version.",
            )

        return self._queue(
            OperationType.LIST_SNAPSHOT_FILES,
            snapshot.plan_id,
            snapshot_id,
        )

    def cached_snapshot_files(
        self,
        snapshot_id: str,
    ) -> list[str] | None:
        snapshot = self.repo.snapshot(snapshot_id)

        if snapshot.state != "committed":
            raise AppError(
                "SNAPSHOT_NOT_COMMITTED",
                "The version is unavailable.",
            )

        if snapshot.source_kind != SourceKind.FOLDER.value:
            raise AppError(
                "RESTORE_DESTINATION_INVALID",
                "File selection is available only for a folder version.",
            )

        cached = self._snapshot_files_cache

        if (
            cached is None
            or cached[0] != snapshot_id
            or cached[1] != snapshot.manifest_sha256
        ):
            return None

        return list(cached[2])

    def load_snapshot_files_for_test(
        self,
        snapshot_id: str,
    ) -> None:
        if self.worker is not None:
            raise AppError(
                "TEST_REQUIRES_WORKERLESS_CONTROLLER",
                "This method is allowed only in a workerless self-test.",
            )

        snapshot = self.repo.snapshot(snapshot_id)
        op = self.repo.create_operation(
            OperationType.LIST_SNAPSHOT_FILES,
            snapshot.plan_id,
            snapshot_id,
            {},
        )

        result = self.verifier.list_snapshot_files(
            op,
            snapshot_id,
            lambda event: None,
            lambda: False,
        )

        if not result.success:
            raise AppError(
                result.error_code or "SNAPSHOT_FILES_NOT_READY",
                result.message,
            )

        data = result.result_data
        self._snapshot_files_cache = (
            snapshot_id,
            data["manifest_sha256"],
            tuple(data["relative_paths"]),
        )

    def _queue(
        self,
        kind: OperationType,
        plan_id: str | None = None,
        snapshot_id: str | None = None,
        trigger_reason: TriggerReason = TriggerReason.MANUAL,
        **payload: Any,
    ) -> str:
        if self._shutdown_requested or self._shutdown_completed:
            raise AppError(
                "APP_SHUTTING_DOWN",
                "The application is shutting down. "
                "New operations are not allowed.",
            )

        reserved = {
            "kind",
            "operation_id",
            "plan_id",
            "snapshot_id",
            "trigger_reason",
            "_cancel_event",
        }
        if reserved.intersection(payload):
            raise AppError(
                "OPERATION_PAYLOAD_INVALID",
                "The payload contains reserved command fields.",
            )

        if (
            plan_id
            and kind != OperationType.DELETE_PLAN_DATA
            and self.repo.plan_deletion_job(plan_id)
        ):
            raise AppError(
                "PLAN_DELETE_RESUME_REQUIRED",
                "This plan has an unfinished deletion.",
            )

        if self.active_operation_id:
            raise AppError(
                "OPERATION_ALREADY_RUNNING",
                "Another heavy operation is running now.",
            )

        if self.worker is None:
            raise AppError(
                "WORKER_DISABLED",
                "The background worker is disabled.",
            )

        operation_id = self.repo.create_operation(
            kind,
            plan_id,
            snapshot_id,
            {
                **payload,
                "trigger_reason": trigger_reason.value,
            },
        )

        token = threading.Event()
        self._active_cancel_event = token
        self.active_operation_id = operation_id

        if kind in {
            OperationType.CHECK_DATABASE,
            OperationType.LIST_SNAPSHOT_FILES,
            OperationType.RUNTIME_TICK,
            OperationType.REFRESH_HEALTH,
        }:
            self._background_queries[operation_id] = (
                kind.value,
                snapshot_id,
            )

        self.request_execute.emit(
            {
                **payload,
                "kind": kind.value,
                "operation_id": operation_id,
                "plan_id": plan_id,
                "snapshot_id": snapshot_id,
                "trigger_reason": trigger_reason.value,
                "_cancel_event": token,
            }
        )
        return operation_id

    def confirm_source_identity(self, plan_id: str) -> None:
        if self.repo.plan_deletion_job(plan_id):
            raise AppError("PLAN_DELETE_RESUME_REQUIRED", "The source cannot be changed while a plan is being deleted.")
        if self.active_operation_id:
            raise AppError("OPERATION_ALREADY_RUNNING", "Wait for the current operation to finish.")
        self.repo.confirm_source_identity(plan_id)

    def queue_backup(
        self,
        plan_id: str,
        force: bool,
        trigger_reason: TriggerReason = TriggerReason.MANUAL,
    ) -> str:
        if trigger_reason not in {
            TriggerReason.MANUAL,
            TriggerReason.MANUAL_FORCE,
            TriggerReason.EVENT,
            TriggerReason.SCHEDULE,
            TriggerReason.STARTUP_CATCHUP,
        }:
            raise AppError(
                "TRIGGER_REASON_INVALID",
                "An unsupported backup reason was passed.",
            )

        effective_force = (
            force
            or trigger_reason == TriggerReason.MANUAL_FORCE
        )

        return self._queue(
            OperationType.BACKUP,
            plan_id,
            force=effective_force,
            trigger_reason=trigger_reason,
        )

    def process_automatic_pending(
        self,
        now_utc_ms: int,
        now_local: datetime,
    ) -> list[AutoDecision]:
        active_plan_id: str | None = None

        if self.active_operation_id:
            try:
                active_plan_id = self.repo.operation_plan_id(
                    self.active_operation_id
                )
            except AppError:
                logging.exception(
                    "Unable to resolve active operation plan"
                )

        decisions = self.auto_coordinator.evaluate_pending(
            now_utc_ms,
            now_local,
            active_plan_id,
        )

        for decision in decisions:
            if decision.action != "queued":
                continue

            plan = self.repo.plan_details(decision.plan_id)

            if plan.pending_trigger_mask & TRIGGER_MASK_EVENT:
                reason = TriggerReason.EVENT
            elif plan.pending_trigger_mask & TRIGGER_MASK_SCHEDULE:
                reason = TriggerReason.SCHEDULE
            else:
                reason = TriggerReason.STARTUP_CATCHUP

            try:
                operation_id = self.queue_backup(
                    decision.plan_id,
                    False,
                    reason,
                )

                self.repo.activity(
                    "info",
                    "AUTO_BACKUP_STARTED",
                    "The automatic backup was queued.",
                    decision.plan_id,
                    operation_id,
                    {
                        "trigger_reason": reason.value,
                        "trigger_mask": decision.trigger_mask,
                    },
                )

            except AppError as exc:
                if exc.code != "OPERATION_ALREADY_RUNNING":
                    self.health_service.record_error(
                        exc,
                        decision.plan_id,
                    )

                logging.info(
                    "Automatic backup was not queued for plan %s: %s",
                    decision.plan_id,
                    exc.code,
                )

        return decisions

    def queue_recovery(self, plan_id: str) -> str:
        self.repo.plan_details(plan_id)
        if self.repo.plan_deletion_job(plan_id):
            self.repo.discard_plan_deletion_job(plan_id)
        if not self.repo.needs_recovery_for_plan(plan_id):
            raise AppError("RECOVERY_NOT_REQUIRED", "The plan has no unfinished operations.")
        return self._queue(OperationType.RECOVER_PLAN, plan_id)

    def restore_test_settings19(self) -> dict:
        try:
            days = int(self.repo.get_setting("recovery19.auto_days", 7))
        except (TypeError, ValueError):
            days = 7
        try:
            max_bytes = int(self.repo.get_setting("recovery19.auto_max_bytes", 512 * 1024 * 1024))
        except (TypeError, ValueError):
            max_bytes = 512 * 1024 * 1024
        return {"enabled": self.repo.get_setting("recovery19.auto_enabled", False) is True,
                "days": min(90, max(1, days)), "max_bytes": max(1024 * 1024, max_bytes)}

    def save_restore_test_settings19(self, enabled: bool, days: int, max_mib: int | None = None) -> None:
        if not 1 <= int(days) <= 90:
            raise AppError("SETTINGS_INVALID", "The check interval must be from 1 to 90 days.")
        if max_mib is not None and not 16 <= int(max_mib) <= 1024 * 1024:
            raise AppError("SETTINGS_INVALID", "The full test limit must be from 16 MiB to 1 TiB.")
        self.repo.set_setting("recovery19.auto_enabled", bool(enabled))
        self.repo.set_setting("recovery19.auto_days", int(days))
        if max_mib is not None:
            self.repo.set_setting("recovery19.auto_max_bytes", int(max_mib) * 1024 * 1024)

    def auto_restore_test_candidate(self) -> tuple[str, str] | None:
        """Pick the snapshot whose latest test is oldest. Full test when it fits the limit,
        otherwise a limited rotating sample."""
        settings = self.restore_test_settings19()
        if (not settings["enabled"] or self.active_operation_id
                or getattr(self, "_shutdown_requested", False)):
            return None
        now, interval = utc_ms(), settings["days"] * 86_400_000
        best: tuple[int, str, str] | None = None
        for summary in self.repo.plan_summaries(False):
            snapshots = self.repo.available_snapshots(summary.id)
            if not snapshots:
                continue
            latest = snapshots[0]
            if not latest.storage_path or not os.path.isdir(latest.storage_path):
                continue
            status = self.restore_test_service.status(latest)
            stamps = [value for value in (
                (status["proof"] or {}).get("tested_at_utc_ms"),
                (status["attempt"] or {}).get("finished_at_utc_ms")) if value]
            last = max(stamps) if stamps else 0
            if last and now - last < interval:
                continue
            mode = "full" if int(latest.logical_size_bytes or 0) <= settings["max_bytes"] else "sample"
            if best is None or last < best[0]:
                best = (last, latest.id, mode)
        return (best[1], best[2]) if best else None

    def queue_test_restore(self, snapshot_id: str, mode: str = "full") -> str:
        snapshot = self.repo.snapshot(snapshot_id)
        if snapshot.state != "committed":
            raise AppError("SNAPSHOT_NOT_COMMITTED", "The version is unavailable for the test.")
        return self._queue(OperationType.TEST_RESTORE, snapshot.plan_id, snapshot_id,
                           mode="sample" if mode == "sample" else "full")

    def queue_verify(self, snapshot_id: str) -> str:
        snapshot = self.repo.snapshot(snapshot_id)
        if snapshot.state != "committed":
            raise AppError(
                "SNAPSHOT_NOT_COMMITTED",
                "Only an available version can be verified.",
            )
        return self._queue(
            OperationType.VERIFY, snapshot.plan_id, snapshot_id
        )

    def queue_restore(self, snapshot_id: str) -> str:
        snapshot = self.repo.snapshot(snapshot_id)
        if snapshot.state != "committed":
            raise AppError(
                "SNAPSHOT_NOT_COMMITTED",
                "Only an available version can be restored.",
            )
        return self._queue(
            OperationType.RESTORE_BESIDE, snapshot.plan_id, snapshot_id
        )

    def queue_replace_original_file(self, snapshot_id: str) -> str:
        snapshot = self.repo.snapshot(snapshot_id)
        if snapshot.state != "committed":
            raise AppError(
                "SNAPSHOT_NOT_COMMITTED",
                "A file can be replaced only from an available version.",
            )
        if snapshot.source_kind != SourceKind.FILE.value:
            raise AppError(
                "RESTORE_REPLACE_INVALID",
                "Replacing a folder is not supported for this version.",
            )

        source = Path(snapshot.source_path)
        if (
            not source.is_absolute()
            or source.is_symlink()
            or not source.is_file()
        ):
            raise AppError(
                "RESTORE_REPLACE_INVALID",
                "The original file is missing or unavailable.",
            )

        return self._queue(
            OperationType.RESTORE_REPLACE_FILE,
            snapshot.plan_id,
            snapshot_id,
        )

    def queue_restore_to_directory(self, snapshot_id: str, directory: str) -> str:
        snapshot = self.repo.snapshot(snapshot_id)
        if snapshot.state != "committed":
            raise AppError("SNAPSHOT_NOT_COMMITTED", "Only an available version can be restored.")
        if not directory or not directory.strip():
            raise AppError("RESTORE_DESTINATION_MISSING", "No destination folder selected.")
        return self._queue(
            OperationType.RESTORE_TO_DIRECTORY,
            snapshot.plan_id,
            snapshot_id,
            target_directory=directory,
        )

    def queue_restore_selected_files(
        self,
        snapshot_id: str,
        selected_relative_paths: list[str],
        directory: str,
    ) -> str:
        snapshot = self.repo.snapshot(snapshot_id)

        if snapshot.state != "committed":
            raise AppError(
                "SNAPSHOT_NOT_COMMITTED",
                "Files can be restored only "
                "from an available saved version.",
            )

        if snapshot.source_kind != SourceKind.FOLDER.value:
            raise AppError(
                "RESTORE_FOLDER_ONLY",
                "Selective restore is available "
                "only for a folder version.",
            )

        if not isinstance(selected_relative_paths, list):
            raise AppError(
                "OPERATION_PAYLOAD_INVALID",
                "The list of selected files has an invalid format.",
            )

        normalized_paths: list[str] = []
        seen: set[str] = set()

        for value in selected_relative_paths:
            if not isinstance(value, str):
                raise AppError(
                    "OPERATION_PAYLOAD_INVALID",
                    "The list of selected files has an invalid format.",
                )

            relative_path = value.replace("\\", "/")

            if (
                not relative_path
                or relative_path.startswith("/")
                or relative_path.startswith("\\")
                or ":" in relative_path.split("/")[0]
                or ".." in relative_path.split("/")
            ):
                raise AppError(
                    "MANIFEST_INVALID",
                    "An unsafe file path was selected.",
                )

            if relative_path not in seen:
                seen.add(relative_path)
                normalized_paths.append(relative_path)

        if not normalized_paths:
            raise AppError(
                "RESTORE_NOTHING_SELECTED",
                "No files selected for restoring.",
            )

        if not directory or not directory.strip():
            raise AppError(
                "RESTORE_DESTINATION_MISSING",
                "No destination folder selected.",
            )

        return self._queue(
            OperationType.RESTORE_SELECTED_FILES,
            snapshot.plan_id,
            snapshot_id,
            selected_relative_paths=normalized_paths,
            target_directory=directory,
        )

    def plan_deletion_preview(self, plan_id: str) -> PlanDeletionPreview:
        preview = self.repo.plan_deletion_preview(plan_id)
        try:
            plan = self.repo.plan_details(plan_id)
            inventory = self.delete_service._build_plan_delete_inventory(plan, lambda: False)
        except AppError:
            return preview
        return replace(
            preview,
            warnings=list(inventory.get("warnings") or []),
            has_problems=bool(inventory.get("has_problems")),
        )

    def queue_delete_plan_data(self, plan_id: str, preview_token: str, approve_pinned: bool = False) -> str:
        if self.active_operation_id:
            raise AppError("OPERATION_ALREADY_RUNNING", "Wait for the heavy operation to finish.")
        preview = self.repo.plan_deletion_preview(plan_id)
        if preview.token != preview_token:
            raise AppError("PLAN_DELETE_CHANGED", "The plan changed. Confirm the deletion again.")
        if preview.pinned_count and not approve_pinned:
            raise AppError("SNAPSHOT_PINNED", "Deleting pinned versions needs a separate confirmation.")
        return self._queue(OperationType.DELETE_PLAN_DATA, plan_id,
                           preview_token=preview_token, approve_pinned=bool(approve_pinned))
    
    def queue_delete(self, snapshot_ids: list[str]) -> str:
        if not snapshot_ids:
            raise AppError(
                "SNAPSHOT_NOT_FOUND",
                "Select versions to delete.",
            )
        snapshot = self.repo.snapshot(snapshot_ids[0])
        if snapshot.state != "committed":
            raise AppError(
                "SNAPSHOT_NOT_COMMITTED",
                "Only available versions can be deleted.",
            )
        return self._queue(
            OperationType.DELETE_SNAPSHOTS,
            snapshot.plan_id,
            snapshot_ids=snapshot_ids,
        )

    def archive_plan(self, plan_id: str) -> None:
        if self.repo.plan_deletion_job(plan_id):
            raise AppError("PLAN_DELETE_RESUME_REQUIRED", "The plan is already being deleted.")
        if self.active_operation_id:
            raise AppError("OPERATION_ALREADY_RUNNING", "A plan cannot be archived during an operation.")
        operation_id = self.repo.create_operation(OperationType.ARCHIVE_PLAN, plan_id, None, {})
        self.repo.operation_state(operation_id, "running")
        self.repo.archive(plan_id, operation_id)

    def set_pin(self, snapshot_id: str, pinned: bool) -> None:
        self.repo.set_pin(snapshot_id, pinned)

    def dismiss_issue(
        self,
        dedupe_key: str,
    ) -> None:
        self.repo.dismiss_issue(dedupe_key)

    def run_automatic_pending_synchronously_for_test(
        self,
        now_utc_ms: int,
        now_local: datetime,
    ) -> list[AutoDecision]:
        if self.worker is not None:
            raise AppError(
                "TEST_SYNC_RUN_REQUIRES_WORKERLESS_CONTROLLER",
                "A synchronous test run is available only with start_worker=False.",
            )

        decisions = self.auto_coordinator.evaluate_pending(
            now_utc_ms,
            now_local,
            None,
        )

        for decision in decisions:
            if decision.action != "queued":
                continue

            plan = self.repo.plan_details(decision.plan_id)

            if plan.pending_trigger_mask & TRIGGER_MASK_EVENT:
                reason = TriggerReason.EVENT
            elif plan.pending_trigger_mask & TRIGGER_MASK_SCHEDULE:
                reason = TriggerReason.SCHEDULE
            else:
                reason = TriggerReason.STARTUP_CATCHUP

            operation_id = self.repo.create_operation(
                OperationType.BACKUP,
                plan.id,
                None,
                {
                    "force": False,
                    "trigger_reason": reason.value,
                },
            )

            result = self.snapshot_service.create(
                operation_id,
                plan.id,
                False,
                lambda event: None,
                lambda: False,
            )

            result.trigger_reason = reason.value
            result.plan_id = plan.id

            if result.success and result.error_code is None:
                self.auto_coordinator.complete_automatic_run(
                    plan.id,
                    now_utc_ms,
                    True,
                )
            else:
                self.repo.activity(
                    "warning",
                    "AUTO_BACKUP_FAILED",
                    "The automatic backup did not finish; the pending trigger was kept.",
                    plan.id,
                    operation_id,
                    {
                        "trigger_reason": reason.value,
                        "error_code": result.error_code,
                    },
                )

        return decisions

    def cancel_active(self) -> None:
        if not self.active_operation_id:
            return

        token = getattr(self, "_active_cancel_event", None)
        if token is not None:
            token.set()
        elif self.worker is not None:
            self.worker.cancel()

def ensure_test_qapplication() -> QApplication:
    """Creates the QApplication required by UI and integration tests."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance()
    if application is None:
        application = QApplication(sys.argv)
    return application


def _external_test_path() -> Path:
    configured = os.environ.get("AMVIBACKUP_TEST_MODULE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).with_name(f"{Path(__file__).stem}_tests.py")


def run_self_tests(verbose: bool = False) -> int:
    """Loads self-tests only on explicit request and returns a process code."""
    test_path = _external_test_path()
    if not test_path.is_file():
        print(
            "Self-test file not found: "
            f"{test_path}. Set the path via AMVIBACKUP_TEST_MODULE.",
            file=sys.stderr,
        )
        return 2

    try:
        import importlib.util

        module_name = "amvibackup_18_external_tests"
        spec = importlib.util.spec_from_file_location(module_name, test_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create a loader for {test_path}")
        module = importlib.util.module_from_spec(spec)
        module.AMVI_APP_MODULE = sys.modules[__name__]
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        runner = getattr(module, "run_self_tests", None)
        if not callable(runner):
            raise AttributeError("run_self_tests() is missing")
        return int(runner(verbose))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        print(
            f"Could not load or run the self-tests: {test_path}",
            file=sys.stderr,
        )
        return 2


def run_gui() -> int:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    app.setApplicationName(APP_NAME)
    set_app_user_model_id()
    app.setWindowIcon(build_app_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(APP_QSS + build_indicator_qss())

    instance_guard: ApplicationInstanceGuard | None = None
    activation: SingleInstanceServer | None = None
    app_mutex = InstallerMutex()
    controller: AppController | None = None
    window: MainWindow | None = None
    logging_ready = False

    try:
        paths = AppPaths.create()
        activation_name = single_instance_name(paths.db_path)
        if request_existing_instance(activation_name):
            running_version = last_existing_instance_version()
            if running_version != APP_VERSION:
                QMessageBox.warning(None, APP_NAME, other_version_message(running_version))
            return 0
        app_mutex.acquire()
        instance_guard = ApplicationInstanceGuard(paths.db_path)
        try:
            instance_guard.acquire()
        except AppError as exc:
            if (
                exc.code == "APP_ALREADY_RUNNING"
                and request_existing_instance_with_retries(activation_name)
            ):
                return 0
            raise
        setup_logging(paths)
        logging_ready = True
        logging.info("Started %s %s (pid %s) from %s", APP_NAME, APP_VERSION, os.getpid(), Path(__file__).resolve())
        controller = AppController(paths)
        apply_installer_language(paths, controller.repo, controller.i18n)
        register_support_first_launch(paths, controller.repo)
        apply_layout_direction(controller.i18n.language)
        window = MainWindow(controller, paths)
        controller.main_window = window
        activation = SingleInstanceServer(activation_name, window)
        activation.activation_requested.connect(window.bring_to_front)
        activation.start()

        try:
            controller.start_automation_runtime()
        except AppError as exc:
            logging.exception("Unable to start automation runtime")
            QMessageBox.warning(
                window,
                APP_NAME,
                controller.i18n.tr(
                    "startup.automation_failed",
                    error=controller.i18n.error_message(exc),
                ),
            )

        tray_started = window.setup_tray_runtime()
        try:
            start_minimized = controller.repo.get_bool_setting(
                SETTING_START_MINIMIZED,
                DEFAULT_START_MINIMIZED,
            )
        except AppError:
            logging.exception("Unable to load start-minimized setting")
            start_minimized = False

        if not (tray_started and start_minimized):
            window.show()
            QTimer.singleShot(400, window.maybe_show_first_launch)
        return app.exec()

    except AppError as exc:
        if logging_ready:
            logging.exception("Application startup/runtime failure")
        QMessageBox.critical(
            None,
            APP_NAME,
            startup_error_text(exc, startup_language(controller)),
        )
        return 1
    except Exception:
        if logging_ready:
            logging.exception("Application startup/runtime failure")
        QMessageBox.critical(
            None,
            APP_NAME,
            dictionary_text("startup.failed", startup_language(controller)),
        )
        return 1
    finally:
        try:
            if activation is not None:
                activation.stop()
            if controller is not None:
                controller.stop_automation_runtime()
                controller.cancel_active()
                thread = controller.thread
                if thread is not None:
                    thread.quit()
                    thread.wait()
                controller.db.checkpoint_wal()
                if window is not None and window.tray_runtime is not None:
                    window.tray_runtime.stop()
        finally:
            if instance_guard is not None:
                instance_guard.release()
            app_mutex.release()
            start_pending_uninstaller()


def run_smoke_test() -> int:
    """Build check for the packaged exe: the dictionary is found, complete and every language is present."""
    problems = list(LANGUAGE_DATA.validate())
    missing = {item.value for item in Language} - set(TRANSLATIONS)
    if missing:
        problems.append(f"missing languages: {sorted(missing)}")
    try:
        from amvibackup_support import check_support_resources
        problems.extend("support resource: " + name for name in check_support_resources())
    except Exception as exc:
        problems.append("Support module failed: " + str(exc))
    if problems:
        if sys.stderr is not None:
            print("\n".join(problems), file=sys.stderr)
        return 2
    if sys.stdout is not None:
        print(f"{APP_NAME} {APP_VERSION} smoke test OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.smoke_test:
        return run_smoke_test()
    if args.self_test:
        return run_self_tests(args.verbose)
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
