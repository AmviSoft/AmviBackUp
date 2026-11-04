# -*- coding: utf-8 -*-
"""AmviBackUp support window. Public contacts and receiving addresses only; no private keys.
Copyright (c) 2026 Maxim Dubkov (AmviSoft). MIT License.
"""
from __future__ import annotations
import hashlib
import sys
from pathlib import Path
from typing import Callable
from PySide6.QtCore import Qt, QUrl, QTimer, QObject, QEvent
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QLineEdit, QScrollArea, QWidget, QFrame,
)

SUPPORT_EMAIL = "amvimaxsoft@gmail.com"
SUPPORT_TELEGRAM = "AmviMax"
SUPPORT_TELEGRAM_URL = "https://t.me/AmviMax"
SUPPORT_ASSETS = [{'id': 'btc', 'asset': 'Bitcoin (BTC)', 'network': 'Bitcoin', 'address': 'bc1qyjulp8v79l3axhvfvr6ns4z7t26t73m5csm6au', 'qr': 'btc_qr.png', 'qr_payload': 'bitcoin:bc1qyjulp8v79l3axhvfvr6ns4z7t26t73m5csm6au', 'qr_sha256': '14f2edf34194aaffabd4b3a1635917d3d7d13cb4fab43b8726ea94e9eb773bcb'}, {'id': 'trx', 'asset': 'TRON (TRX)', 'network': 'TRON', 'address': 'TV28nSwhfjQvj64YoyVoDJvArguYaJJGbM', 'qr': 'trx_qr.png', 'qr_payload': 'TV28nSwhfjQvj64YoyVoDJvArguYaJJGbM', 'qr_sha256': '64be5bd617d43dda7d84329ccfcb8aa2837f260f7777842d5f198d98f43d58ab'}, {'id': 'eth', 'asset': 'Ethereum (ETH)', 'network': 'Ethereum', 'address': '0x2E3491CeD3B1409E0d0Bf8c54Fd61Cd067172AAb', 'qr': 'eth_qr.png', 'qr_payload': '0x2E3491CeD3B1409E0d0Bf8c54Fd61Cd067172AAb', 'qr_sha256': '3eaff10e8c3434a927e126828c1ad3d4b21622e5fff5236028b39285256e702a'}]
SUPPORT_PHOTOS = ['dog_01.jpg', 'dog_02.jpg', 'dog_03.jpg', 'dog_04.jpg']
SUPPORT_RESOURCE_HASHES = {'btc_qr.png': '14f2edf34194aaffabd4b3a1635917d3d7d13cb4fab43b8726ea94e9eb773bcb', 'trx_qr.png': '64be5bd617d43dda7d84329ccfcb8aa2837f260f7777842d5f198d98f43d58ab', 'eth_qr.png': '3eaff10e8c3434a927e126828c1ad3d4b21622e5fff5236028b39285256e702a', 'dog_01.jpg': '75e159318f837525af00c95be8ddee62a993b0f9eaf37c2b1cea97a058609991', 'dog_02.jpg': 'c358fca54c45dddeac8767df5611ffca8df390547285423fd7a9ab03beb38334', 'dog_03.jpg': 'e3c6e00c133ae4d55e9e957b0ca2d2a67c8128fd6b270f3973b87bb5c166081c', 'dog_04.jpg': 'cd7b0c936b9dad5a14ea501d4ffe0b90fae6accf03ebe6f38c77714e99793223'}


def resource_path(filename: str) -> Path:
    """Assets are next to this module in source and inside _MEIPASS in a frozen build."""
    if Path(filename).name != filename:
        raise ValueError("Not a resource filename")
    folders = [Path(__file__).resolve().parent]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        folders.append(Path(bundle))
    for folder in folders:
        candidate = folder / "assets" / "support" / filename
        if candidate.is_file():
            return candidate
    return folders[0] / "assets" / "support" / filename


def check_support_resources() -> list[str]:
    problems = []
    for filename, digest in SUPPORT_RESOURCE_HASHES.items():
        try:
            actual = hashlib.sha256(resource_path(filename).read_bytes()).hexdigest()
        except OSError:
            actual = None
        if actual != digest:
            problems.append(filename)
    return problems


class SupportWheelGuard(QObject):
    """Wheel over a selector/read-only field scrolls the page, never changes the selected asset."""

    def __init__(self, scroll, parent):
        super().__init__(parent)
        self.scroll = scroll

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel:
            return False
        bar = self.scroll.verticalScrollBar()
        pixels = event.pixelDelta().y()
        angle = event.angleDelta().y()
        delta = pixels if pixels else int(angle / 120 * bar.singleStep() * 3)
        if delta:
            bar.setValue(bar.value() - delta)
        event.accept()
        return True


class SupportDialog(QDialog):
    def __init__(self, i18n, parent=None):
        super().__init__(parent)
        self.i18n = i18n
        self.tr_support = i18n.tr
        tr = self.tr_support
        self.setWindowTitle(tr("support.title"))
        self.resize(840, 760)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        self.scroll_area = scroll
        self._wheel_guard = SupportWheelGuard(scroll, self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setSpacing(14)
        self.add_text(body, tr("support.intro"))
        self.heading(body, tr("support.contacts"))
        self.contact_rows = {}
        self.contact_rows["email"] = self.contact_row(
            body, tr("support.email"), SUPPORT_EMAIL, "mailto:" + SUPPORT_EMAIL)
        self.contact_rows["telegram"] = self.contact_row(
            body, tr("support.telegram"), "@" + SUPPORT_TELEGRAM, SUPPORT_TELEGRAM_URL)
        self.heading(body, tr("support.purpose_title"))
        self.add_text(body, tr("support.purpose"))
        self.add_text(body, tr("support.photos_hint"), muted=True)
        self.photo_buttons = []
        grid = QGridLayout()
        for index, filename in enumerate(SUPPORT_PHOTOS):
            button = QPushButton(tr("support.photo", n=index + 1, total=len(SUPPORT_PHOTOS)))
            button.setMinimumHeight(150)
            pixmap = QPixmap(str(resource_path(filename)))
            if not pixmap.isNull():
                from PySide6.QtGui import QIcon
                from PySide6.QtCore import QSize
                button.setIcon(QIcon(pixmap))
                button.setIconSize(QSize(135, 180))
            button.setToolTip(tr("support.photo", n=index + 1, total=len(SUPPORT_PHOTOS)))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, i=index: self.open_photo(i))
            grid.addWidget(button, index // 2, index % 2)
            self.photo_buttons.append(button)
        body.addLayout(grid)
        self.heading(body, tr("support.donate"))
        self.add_text(body, tr("support.asset"))
        self.asset_combo = QComboBox()
        self.asset_combo.installEventFilter(self._wheel_guard)
        self.asset_combo.view().viewport().installEventFilter(self._wheel_guard)
        for asset in SUPPORT_ASSETS:
            self.asset_combo.addItem(asset["asset"] + " — " + asset["network"], asset["id"])
        body.addWidget(self.asset_combo)
        self.network_label = QLabel()
        body.addWidget(self.network_label)
        self.add_text(body, tr("support.address"))
        row = QHBoxLayout()
        self.address_edit = QLineEdit()
        self.address_edit.setReadOnly(True)
        self.address_edit.installEventFilter(self._wheel_guard)
        self.address_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.address_edit.setMinimumWidth(100)
        row.addWidget(self.address_edit, 1)
        self.copy_address_button = QPushButton(tr("support.copy"))
        self.copy_address_button.clicked.connect(self.copy_address)
        row.addWidget(self.copy_address_button)
        body.addLayout(row)
        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumHeight(260)
        body.addWidget(self.qr_label)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        body.addWidget(self.warning_label)
        self.add_text(body, tr("support.qr_hint"), muted=True)
        self.asset_combo.currentIndexChanged.connect(self.update_asset)
        self.update_asset()
        if check_support_resources():
            self.add_text(body, tr("support.assets_missing"))
        self.heading(body, tr("support.business_title"))
        self.add_text(body, tr("support.business"))
        body.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)
        footer = QHBoxLayout()
        footer.addStretch()
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        outer.addLayout(footer)

    @staticmethod
    def heading(layout, text):
        label = QLabel(text)
        label.setProperty("role", "subtitle")
        label.setWordWrap(True)
        layout.addWidget(label)

    @staticmethod
    def add_text(layout, text, muted=False):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        if muted:
            label.setProperty("role", "muted")
        layout.addWidget(label)
        return label

    def contact_row(self, layout, title, value, url):
        tr = self.tr_support
        self.add_text(layout, title)
        row = QHBoxLayout()
        text = QLineEdit(value)
        text.setReadOnly(True)
        text.installEventFilter(self._wheel_guard)
        text.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        row.addWidget(text, 1)
        open_button = QPushButton(tr("support.open"))
        open_button.clicked.connect(lambda: self.open_contact(url))
        copy_button = QPushButton(tr("support.copy"))
        copy_button.clicked.connect(lambda: self.copy_text(value))
        row.addWidget(open_button)
        row.addWidget(copy_button)
        layout.addLayout(row)
        return text, open_button, copy_button

    def open_contact(self, url):
        # Only called after an explicit click; no network activity when opening the dialog.
        if not QDesktopServices.openUrl(QUrl(url)):
            self.status_label.setText(self.tr_support("support.open_failed"))

    def copy_text(self, text):
        QApplication.clipboard().setText(text)
        self.status_label.setText(self.tr_support("support.copied"))

    def copy_address(self):
        self.copy_text(SUPPORT_ASSETS[self.asset_combo.currentIndex()]["address"])

    def update_asset(self, _index=None):
        asset = SUPPORT_ASSETS[self.asset_combo.currentIndex()]
        tr = self.tr_support
        self.address_edit.setText(asset["address"])
        self.network_label.setText(tr("support.network", network=asset["network"]))
        self.warning_label.setText(tr("support.warning", asset=asset["asset"], network=asset["network"]))
        path = resource_path(asset["qr"])
        try:
            raw = path.read_bytes()
        except OSError:
            raw = b""
        if hashlib.sha256(raw).hexdigest() != asset["qr_sha256"]:
            self.qr_label.setText(tr("support.assets_missing"))
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw, "PNG"):
            self.qr_label.setText(tr("support.assets_missing"))
            return
        # Integer pixels per module, quiet zone preserved; do not resample the QR image.
        self.qr_label.setPixmap(pixmap)

    def open_photo(self, index):
        filename = SUPPORT_PHOTOS[index]
        viewer = QDialog(self)
        viewer.setWindowTitle(self.tr_support("support.photo", n=index + 1, total=len(SUPPORT_PHOTOS)))
        viewer.resize(720, 780)
        layout = QVBoxLayout(viewer)
        image = QLabel()
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(str(resource_path(filename)))
        image.setPixmap(pixmap.scaled(660, 680, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(image)
        close = QPushButton(self.tr_support("common.close"))
        close.clicked.connect(viewer.accept)
        layout.addWidget(close)
        try:
            viewer.exec()
        finally:
            viewer.deleteLater()
