"""
9router_WatchEdit - Security Indicator & Controls
Visible security state (SECRETS: LOCKED / OS VAULT / UNLOCKED) with a dialog
offering unlock, lock, trust toggle, and safe diagnostic bundle export.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core.security import LOCKED, OS_VAULT, SecurityManager
from core.secret_store import VaultStore
from ui.theme import COLOR_BORDER_HIGHLIGHT, COLOR_ACCENT_TEAL, COLOR_DANGER, COLOR_TEXT_SECONDARY


STATE_LABELS = {
    LOCKED: "SECRETS: LOCKED",
    OS_VAULT: "SECRETS: OS VAULT",
    "UNLOCKED": "SECRETS: UNLOCKED",
}

STATE_COLORS = {
    LOCKED: COLOR_DANGER,
    OS_VAULT: COLOR_ACCENT_TEAL,
    "UNLOCKED": COLOR_BORDER_HIGHLIGHT,
}


class SecurityIndicator(QPushButton):
    """Header chip showing the current security state; click opens controls."""

    def __init__(self, security: SecurityManager, parent=None):
        super().__init__(parent)
        self.security = security
        self.setFlat(True)
        self.setCursor(Qt.PointingHandCursor)
        self._refresh()
        security.on_state_changed(lambda _s: self._refresh())

    def _refresh(self):
        state = self.security.state
        self.setText(STATE_LABELS.get(state, f"SECRETS: {state}"))
        color = STATE_COLORS.get(state, COLOR_TEXT_SECONDARY)
        self.setStyleSheet(f"color: {color}; font-weight: bold; border: 1px solid {color};")


class SecurityDialog(QDialog):
    def __init__(self, security: SecurityManager, parent=None, on_unlock_callback=None):
        super().__init__(parent)
        self.security = security
        self.on_unlock_callback = on_unlock_callback
        self.setWindowTitle("Security Controls")
        self.setMinimumWidth(460)
        self._setup_ui()
        self._refresh()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.lbl_state = QLabel()
        self.lbl_state.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(self.lbl_state)

        self.lbl_hint = QLabel()
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 10px;")
        layout.addWidget(self.lbl_hint)

        # OS-backed unlock row
        row_os = QHBoxLayout()
        self.btn_unlock_os = QPushButton("Unlock Live Access (this machine)")
        self.btn_unlock_os.clicked.connect(self._unlock_os)
        row_os.addWidget(self.btn_unlock_os)
        self.chk_trust = QCheckBox("Trust this machine (auto-unlock on startup)")
        self.chk_trust.toggled.connect(self._on_trust_toggled)
        self.chk_trust.setChecked(self.security.trusted_os_unlock_enabled)
        row_os.addWidget(self.chk_trust)
        layout.addLayout(row_os)

        # Vault unlock row
        row_vault = QHBoxLayout()
        self.txt_vault_password = QLineEdit()
        self.txt_vault_password.setEchoMode(QLineEdit.Password)
        self.txt_vault_password.setPlaceholderText("Vault master password (optional)")
        row_vault.addWidget(self.txt_vault_password)
        self.btn_unlock_vault = QPushButton("Unlock with Password")
        self.btn_unlock_vault.clicked.connect(self._unlock_vault)
        row_vault.addWidget(self.btn_unlock_vault)
        layout.addLayout(row_vault)
        self.lbl_vault_status = QLabel()
        self.lbl_vault_status.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 10px;")
        layout.addWidget(self.lbl_vault_status)

        # Lock + diagnostics row
        row_lock = QHBoxLayout()
        self.btn_lock = QPushButton("Lock Now")
        self.btn_lock.setObjectName("dangerAction")
        self.btn_lock.clicked.connect(self._lock)
        row_lock.addWidget(self.btn_lock)
        self.btn_diag = QPushButton("Export Diagnostic Bundle...")
        self.btn_diag.clicked.connect(self._export_diagnostics)
        row_lock.addWidget(self.btn_diag)
        row_lock.addStretch()
        layout.addLayout(row_lock)

        self.lbl_threat = QLabel(
            "Development / external-agent mode keeps secrets LOCKED: UI, presets and "
            "cached models work; live provider operations are refused. "
            "Locking again stops all live access immediately."
        )
        self.lbl_threat.setWordWrap(True)
        self.lbl_threat.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 10px;")
        layout.addWidget(self.lbl_threat)

    def _refresh(self):
        state = self.security.state
        self.lbl_state.setText(STATE_LABELS.get(state, f"SECRETS: {state}"))
        color = STATE_COLORS.get(state, COLOR_TEXT_SECONDARY)
        self.lbl_state.setStyleSheet(f"font-weight: bold; font-size: 12px; color: {color};")
        if state == LOCKED:
            self.lbl_hint.setText(
                "Live provider operations are disabled. Non-secret functionality "
                "(UI, presets, cached model list) keeps working."
            )
        elif state == OS_VAULT:
            self.lbl_hint.setText("Unlocked via OS-backed grant for this Windows user. Live operations available.")
        else:
            names = ", ".join(self.security.vault_secret_names) or "none"
            self.lbl_hint.setText(f"Unlocked via password vault. Vault entries: {names}")
        vault_state = (
            "Vault file present: %s" % self.security.vault_exists()
            if self.security.vault_exists()
            else "No password vault configured (OS-backed unlock is available)."
        )
        if not VaultStore.available():
            vault_state += " [vault libraries not installed]"
        self.lbl_vault_status.setText(vault_state)
        self.btn_unlock_os.setEnabled(state == LOCKED)
        self.btn_unlock_vault.setEnabled(state == LOCKED and self.security.vault_exists())
        self.txt_vault_password.setEnabled(self.btn_unlock_vault.isEnabled())
        self.btn_lock.setEnabled(state != LOCKED)

    # ----------------------------------------------------------------- actions
    def _unlock_os(self):
        try:
            self.security.unlock_os()
        except Exception as ex:
            QMessageBox.critical(self, "Unlock Failed", f"OS-backed unlock failed: {ex}")
        self._refresh()
        if self.on_unlock_callback:
            self.on_unlock_callback()

    def _unlock_vault(self):
        pw = self.txt_vault_password.text()
        self.txt_vault_password.clear()
        if not pw:
            return
        try:
            self.security.unlock_with_password(pw)
        except Exception as ex:
            QMessageBox.critical(self, "Vault Unlock Failed", str(ex))
        self._refresh()
        if self.on_unlock_callback:
            self.on_unlock_callback()

    def _lock(self):
        self.security.lock()
        self._refresh()

    def _on_trust_toggled(self, checked: bool):
        self.security.set_trusted_os_unlock(bool(checked))

    def _export_diagnostics(self):
        try:
            from core.diagnostics import export_diagnostic_bundle
            path = export_diagnostic_bundle()
            QMessageBox.information(
                self, "Diagnostic Bundle",
                f"Redacted diagnostic bundle written to:\n{path}\n\n"
                "It contains no credentials and may be shared with external agents.",
            )
        except Exception as ex:
            QMessageBox.critical(self, "Diagnostic Export Failed", str(ex))
