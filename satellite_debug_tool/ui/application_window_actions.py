"""Shared customer directory and application-update window actions."""
from PySide6.QtWidgets import QDialog, QMessageBox
from satellite_debug_tool import __version__
from satellite_debug_tool.core.customer import CustomerDeviceDirectoryError
from satellite_debug_tool.ui.customer_device_dialog import CustomerDeviceDialog
from satellite_debug_tool.ui.update_dialog import UpdateDialog, silent_background_check
from satellite_debug_tool.updater.status import write_update_status
from satellite_debug_tool.updater.handoff import launch_updater
from satellite_debug_tool.i18n import tr

class ApplicationWindowActions:
    def _show_customer_device_error(self, error: Exception) -> None:
        message = tr("Customer device configuration failed: {detail}", detail=str(error))
        self._on_status_message(message, 5000)
        QMessageBox.warning(self, tr("Customer devices"), message)

    def _on_add_customer_device(self) -> None:
        dialog = CustomerDeviceDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            result = self._customer_devices.add(dialog.endpoint())
        except CustomerDeviceDirectoryError as exc:
            self._show_customer_device_error(exc)
            return
        if result.duplicate_selected:
            self._on_status_message(tr("The existing customer device was selected"), 3000)
        else:
            self._on_status_message(tr("Customer device added"), 2500)

    def _on_edit_customer_device(self, endpoint: object) -> None:
        source = (str(endpoint[0]), int(endpoint[1]))
        dialog = CustomerDeviceDialog(source, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            result = self._customer_devices.edit(source, dialog.endpoint())
        except CustomerDeviceDirectoryError as exc:
            self._show_customer_device_error(exc)
            return
        if result.duplicate_selected:
            self._on_status_message(tr("The existing customer device was selected"), 3000)
        elif result.changed:
            self._on_status_message(tr("Customer device updated"), 2500)

    def _on_delete_customer_device(self, endpoint: object) -> None:
        target = (str(endpoint[0]), int(endpoint[1]))
        answer = QMessageBox.question(
            self,
            tr("Delete customer device"),
            tr(
                "Delete customer device {endpoint}?",
                endpoint=f"{target[0]}:{target[1]}",
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._customer_devices.delete(target)
        except CustomerDeviceDirectoryError as exc:
            self._show_customer_device_error(exc)
            return
        self._on_status_message(tr("Customer device deleted"), 2500)

    def _on_check_update_clicked(self):
        """点击 🔄 检查更新：手动触发，弹 UpdateDialog 立即开始检查。"""
        dlg = UpdateDialog(self._settings, parent=self, auto_start=True)
        dlg.exec()
        request = dlg.launch_request
        if request is not None:
            self._pending_update_request = request
            self.close()

    def _reject_close(self, event, reason: str) -> None:
        request = self._pending_update_request
        self._pending_update_request = None
        if request is not None:
            try:
                write_update_status(
                    request.status_path,
                    "failed",
                    stage="main_shutdown",
                    detail=reason,
                    target_version=request.target_version,
                    install_dir=str(request.install_dir),
                )
            except OSError:
                pass
        event.ignore()

    def _kick_silent_update_check(self):
        """启动后 2s 调用：根据 settings.update.auto_check 决定是否后台静默检查。"""
        self._bg_check_thread = silent_background_check(
            self._settings,
            parent=self,
            on_new_version=self._on_silent_check_found_new,
        )

    def _shutdown_background_update_check(self, timeout_ms: int = 9000) -> bool:
        """Stop the optional update worker before its parent window is destroyed."""

        thread = self._bg_check_thread
        if thread is None:
            return True
        if not thread.isRunning():
            self._bg_check_thread = None
            return True
        thread.requestInterruption()
        thread.quit()
        if not thread.wait(max(0, int(timeout_ms))):
            return False
        self._bg_check_thread = None
        return True

    def _on_silent_check_found_new(self, latest):
        """后台检查发现新版：状态栏提示，用户点击 / 工具栏按钮可展开 UpdateDialog。"""
        msg = tr(
            "New version {latest} is available (current: v{current}). "
            "Use Check for updates to install it.",
            latest=latest.tag_name,
            current=__version__,
        )
        sb = self.statusBar()
        if sb is not None:
            sb.showMessage(msg, 15000)


    def _shutdown_customer_resources(self, event):
        try:
            self._customer_devices.shutdown()
        except CustomerDeviceDirectoryError as exc:
            QMessageBox.warning(self, tr("Customer devices"), str(exc))
            self._reject_close(event, str(exc))
            return False
        if not self._endpoint_directory.shutdown():
            QMessageBox.warning(
                self,
                tr("Device operation in progress"),
                tr("Shared device sessions still have active owners"),
            )
            self._reject_close(event, "shared device sessions still have active owners")
            return False
        if not self._udp_broker.shutdown():
            QMessageBox.warning(
                self,
                tr("Device operation in progress"),
                tr("The shared UDP transport did not stop cleanly"),
            )
            self._reject_close(event, "shared UDP transport did not stop")
            return False
        return True

    def _launch_pending_update(self):
        request = self._pending_update_request
        self._pending_update_request = None
        if request is not None:
            try:
                launch_updater(request)
            except OSError as exc:
                try:
                    write_update_status(
                        request.status_path,
                        "failed",
                        stage="launching_updater",
                        detail=str(exc),
                        target_version=request.target_version,
                        install_dir=str(request.install_dir),
                    )
                except OSError:
                    pass
                QMessageBox.critical(
                    self,
                    tr("Check for updates"),
                    tr(
                        "Failed to start updater: {detail}\nLog: {log}",
                        detail=exc,
                        log=request.log_path,
                    ),
                )
