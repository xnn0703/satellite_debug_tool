"""Top-level customer, engineering, and production workspace coordinator.

MainWindow owns workspace lifecycle and lazy-page hosts.  The shared live
session remains stable across shortcuts while each presentation runs only when
its page is active.
"""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QWidget,
)

from satellite_debug_tool import __version__
from satellite_debug_tool.core.comm import UdpEndpointBroker
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.core.customer import (
    CustomerDeviceDirectory,
    CustomerDeviceDirectoryError,
)
from satellite_debug_tool.core.session import (
    EndpointSessionDirectory,
)
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.customer_workspace import CustomerWorkspace
from satellite_debug_tool.ui.customer_device_dialog import CustomerDeviceDialog
from satellite_debug_tool.ui.customer_session_bundle import (
    CustomerEndpointSessionBundleFactory,
)
from satellite_debug_tool.ui.device_view import DeviceView
from satellite_debug_tool.ui.engineering_session_host import (
    ENGINEERING_SERIAL,
    ENGINEERING_SHARED_UDP,
    EngineeringSessionHost,
)
from satellite_debug_tool.ui.live_view import LiveView
from satellite_debug_tool.ui.lazy_view_host import LazyViewHost
from satellite_debug_tool.ui.log_view import LogView
from satellite_debug_tool.ui.playback_view import PlaybackView
from satellite_debug_tool.ui.production_workspace import ProductionWorkspace
from satellite_debug_tool.ui.settings_dialog import (
    SETTINGS_SCOPE_CUSTOMER,
    SETTINGS_SCOPE_ENGINEERING,
    SETTINGS_SCOPE_PRODUCTION,
    SettingsDialog,
)
from satellite_debug_tool.ui.tracking_simulator_view import TrackingSimulatorView
from satellite_debug_tool.ui.update_dialog import (
    UpdateDialog,
    silent_background_check,
)
from satellite_debug_tool.ui.view_lifecycle import activate_view, deactivate_view


class MainWindow(QMainWindow):
    _TAB_IDS = ("live", "playback", "log", "device", "tracking_simulator")

    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.setWindowTitle(tr("SoftHertz Phased-Array Terminal Tool"))
        self.resize(1280, 800)
        self.setMinimumWidth(1024)
        self.setMinimumHeight(600)

        self._settings = settings or Settings()
        # M7：一次性清掉历史遗留 settings key（attitude 已迁移到全自动；
        # ui.font_scale 已固化为 small，UI 不再暴露调节入口）
        cleaned = False
        if self._settings.remove("attitude"):
            cleaned = True
        if self._settings.remove("ui.font_scale"):
            cleaned = True
        legacy_tab = self._settings.get("ui.active_tab")
        if legacy_tab is not None:
            legacy_map = {
                "实时": "live",
                "Live": "live",
                "回放": "playback",
                "Playback": "playback",
                "Log": "log",
                "设备": "device",
                "Device": "device",
            }
            self._settings.set(
                "ui.active_tab_id",
                legacy_map.get(str(legacy_tab), "live"),
            )
            if self._settings.remove("ui.active_tab"):
                cleaned = True
        if cleaned:
            self._settings.persist_preferences()

        # 主题：兼容旧 "Dark"/"Light" 大写写法
        theme_raw = self._settings.get("ui.theme", "dark")
        self._theme = self._normalize_theme(theme_raw)
        self._engineering_session_mode = ENGINEERING_SERIAL

        # ---------- 顶部全局 gbar（品牌 + 居中 Tab 药丸 + 右侧控件，Mission Console） ----------
        self._build_global_bar()

        # ---------- 共享 UDP Runtime + 独立工程串口 ----------
        # Customer 与 Production 共用一个 Broker/Directory；工程串口保留独立
        # Core，不能借用或重绑 Customer endpoint。
        self._udp_broker = UdpEndpointBroker(
            local_port=int(self._settings.get("device_udp.local_port", 45678)),
            parent=self,
        )
        self._endpoint_directory = EndpointSessionDirectory(
            self._udp_broker,
            parent=self,
        )
        self._session_registry = self._endpoint_directory
        self._customer_bundle_factory = CustomerEndpointSessionBundleFactory(
            settings=self._settings,
            session_directory=self._endpoint_directory,
            status_sink=self._on_status_message,
            parent=self,
        )
        self._customer_devices = CustomerDeviceDirectory(
            self._settings,
            self._endpoint_directory,
            supplemental_facts_provider=self._customer_bundle_factory,
            parent=self,
        )
        self._customer_bundle_factory.bind_device_directory(self._customer_devices)
        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.North)
        self._tabs.tabBar().hide()   # gbar 药丸接管 tab 切换
        self._tabs.setDocumentMode(True)
        self._live = LiveView(
            settings=self._settings,
            serial_only=True,
            defer_presentation=True,
        )
        self._playback: PlaybackView | None = None
        self._log: LogView | None = None
        self._device: DeviceView | None = None
        self._tracking_simulator: TrackingSimulatorView | None = None
        self._production: ProductionWorkspace | None = None
        self._playback_host = LazyViewHost(self._create_playback_view)
        self._log_host = LazyViewHost(self._create_log_view)
        self._device_host = LazyViewHost(self._create_device_view)
        self._tracking_simulator_host = LazyViewHost(self._create_tracking_simulator_view)
        self._production_host = LazyViewHost(self._create_production_workspace)
        self._engineering_live_host = EngineeringSessionHost(
            "live",
            serial_provider=lambda: self._live,
            device_directory=self._customer_devices,
            bundle_factory=self._customer_bundle_factory,
        )
        self._engineering_device_router = EngineeringSessionHost(
            "device",
            serial_provider=lambda: self._device_host,
            device_directory=self._customer_devices,
            bundle_factory=self._customer_bundle_factory,
        )
        self._engineering_tracking_router = EngineeringSessionHost(
            "tracking",
            serial_provider=lambda: self._tracking_simulator_host,
            device_directory=self._customer_devices,
            bundle_factory=self._customer_bundle_factory,
        )
        self._tabs.addTab(self._engineering_live_host, tr("Live"))
        self._tabs.addTab(self._playback_host, tr("Playback"))
        self._tabs.addTab(self._log_host, "Log")
        self._tabs.addTab(self._engineering_device_router, tr("Device"))
        self._tabs.addTab(self._engineering_tracking_router, tr("Tracking Simulator"))
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._tabs.currentChanged.connect(self._sync_tab_pills)

        self._customer = CustomerWorkspace(
            self._live,
            self._settings,
            self._ensure_device_view,
            device_directory=self._customer_devices,
            page_bundle_factory=self._customer_bundle_factory,
            external_power_store=None,
            external_power_presenter=None,
            iperf_controller=None,
            iperf_store=None,
        )
        self._customer.add_requested.connect(self._on_add_customer_device)
        self._customer.edit_requested.connect(self._on_edit_customer_device)
        self._customer.delete_requested.connect(self._on_delete_customer_device)
        for view in (
            self._customer,
            self._engineering_live_host,
            self._playback_host,
            self._log_host,
            self._engineering_device_router,
            self._engineering_tracking_router,
            self._production_host,
        ):
            view.status_message.connect(self._on_status_message)

        initial_engineering_mode = str(
            self._settings.get("ui.engineering_session_mode", ENGINEERING_SERIAL)
        )
        if initial_engineering_mode not in {
            ENGINEERING_SERIAL,
            ENGINEERING_SHARED_UDP,
        }:
            initial_engineering_mode = ENGINEERING_SERIAL
        self._set_engineering_session_mode(
            initial_engineering_mode,
            persist=False,
        )

        self._workspace = QStackedWidget()
        self._active_workspace_index: int | None = None
        self._active_engineering_tab_index: int | None = None
        self._workspace.addWidget(self._customer)
        self._workspace.addWidget(self._tabs)
        self._workspace.addWidget(self._production_host)
        self._workspace.currentChanged.connect(self._on_workspace_changed)
        self.setCentralWidget(self._workspace)

        # 工程诊断默认不出现在客户导航中。现场工程师可通过快捷键确认后在
        # 当前进程内解锁；该状态不持久化，也不改变设备连接或数据流。
        self._engineering_shortcut = QShortcut(QKeySequence("Ctrl+Shift+E"), self)
        self._engineering_shortcut.activated.connect(self._request_engineering_unlock)
        self._engineering_unlocked = False
        self._production_shortcut = QShortcut(QKeySequence("Ctrl+Shift+P"), self)
        self._production_shortcut.activated.connect(self._request_production_unlock)
        self._production_unlocked = False

        # ---------- 底部 statusbar ----------
        self.setStatusBar(QStatusBar())
        # M11：状态栏右下角显示当前版本号
        self._version_label = QLabel(f"v{__version__}")
        self._version_label.setStyleSheet("padding: 0 8px; color: #888;")
        self.statusBar().addPermanentWidget(self._version_label)

        # ---------- 应用主题 + 恢复 active tab ----------
        self._apply_theme(self._theme)
        active_tab_id = str(self._settings.get("ui.active_tab_id", "live"))
        try:
            self._tabs.setCurrentIndex(self._TAB_IDS.index(active_tab_id))
        except ValueError:
            self._tabs.setCurrentIndex(0)
        self._workspace.setCurrentIndex(0)
        self._on_workspace_changed(self._workspace.currentIndex())

        # M11：启动后台静默检查更新（settings.update.auto_check 控制）
        self._bg_check_thread = None
        QTimer.singleShot(2000, self._kick_silent_update_check)
        register_translatable(self)

    # ============================ Lazy pages ============================

    def _create_playback_view(self) -> PlaybackView:
        self._playback = PlaybackView(settings=self._settings)
        return self._playback

    def _create_log_view(self) -> LogView:
        self._log = LogView(settings=self._settings)
        return self._log

    def _create_device_view(self) -> DeviceView:
        device = DeviceView(
            settings=self._settings,
            session_core=self._live.session_core(),
        )
        device.debug_mode_requested.connect(self._live.request_debug_mode)
        self._live.debug_request_finished.connect(device.on_debug_request_finished)
        self._live.debug_state_changed.connect(device.set_debug_state)
        device.set_debug_state(self._live.is_debug_enabled())
        self._device = device
        return device

    def _create_production_workspace(self) -> ProductionWorkspace:
        self._production = ProductionWorkspace(
            self._settings,
            session_registry=self._session_registry,
            broker=self._udp_broker,
            session_directory=self._endpoint_directory,
        )
        return self._production

    def _create_tracking_simulator_view(self) -> TrackingSimulatorView:
        self._tracking_simulator = TrackingSimulatorView(self._live.session_core())
        return self._tracking_simulator

    def _ensure_device_view(self) -> DeviceView:
        return self._device_host.ensure_view()

    def _ensure_production_workspace(self) -> ProductionWorkspace:
        return self._production_host.ensure_view()

    # ============================ 全局顶栏 ============================

    def _build_global_bar(self):
        """Mission Console 顶栏：左品牌 + 居中 Tab 药丸 + 右侧控件。"""
        from PySide6.QtWidgets import QHBoxLayout, QSizePolicy

        self._toolbar = QToolBar()
        self._toolbar.setMovable(False)
        self._toolbar.setFixedHeight(48)
        self.addToolBar(self._toolbar)

        bar = QWidget()
        bar.setObjectName("gbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 0, 10, 0)
        row.setSpacing(10)

        # 左：品牌（渐变方块 + 卫星图标 + 标题）
        self._brand_mark = QLabel()
        self._brand_mark.setObjectName("brandMark")
        self._brand_mark.setFixedSize(26, 26)
        self._brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._brand_text = QLabel(tr("SoftHertz"))
        self._brand_text.setObjectName("brandText")
        row.addWidget(self._brand_mark)
        row.addWidget(self._brand_text)
        row.addStretch(1)

        # 中：客户模式不显示模式标识；工程模式只显示原来的四个 Tab。
        self._tab_pillbar = QWidget()
        self._tab_pillbar.setObjectName("tabPills")
        pill_row = QHBoxLayout(self._tab_pillbar)
        pill_row.setContentsMargins(3, 3, 3, 3)
        pill_row.setSpacing(2)
        self._engineering_tab_pills: list[QPushButton] = []
        engineering_defs = [
            (tr("Live"), "activity"),
            (tr("Playback"), "history"),
            ("Log", "list"),
            (tr("Device"), "cpu"),
            (tr("Tracking Simulator"), "activity"),
        ]
        for idx, (label, icon_name) in enumerate(engineering_defs):
            button = QPushButton(label)
            button.setObjectName("tabPill")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("iconName", icon_name)
            button.clicked.connect(
                lambda _checked, i=idx: self._select_engineering_tab(i)
            )
            button.hide()
            pill_row.addWidget(button)
            self._engineering_tab_pills.append(button)
        self._tab_pills = self._engineering_tab_pills
        self._tab_pillbar.hide()
        row.addWidget(self._tab_pillbar)
        self._engineering_mode_combo = QComboBox()
        self._engineering_mode_combo.setObjectName("engineeringModeCombo")
        self._engineering_mode_combo.addItem(
            tr("Engineering serial"),
            ENGINEERING_SERIAL,
        )
        self._engineering_mode_combo.addItem(
            tr("Shared customer UDP"),
            ENGINEERING_SHARED_UDP,
        )
        self._engineering_mode_combo.setToolTip(
            tr("Choose an independent serial session or the selected customer UDP session")
        )
        self._engineering_mode_combo.currentIndexChanged.connect(
            self._on_engineering_mode_changed
        )
        self._engineering_mode_combo.hide()
        row.addWidget(self._engineering_mode_combo)
        row.addStretch(1)

        # 右：主题切换 + 检查更新 + 设置（图标按钮）
        self._theme_btn = QPushButton()
        self._theme_btn.setObjectName("iconBtn")
        self._theme_btn.setFixedSize(30, 30)
        self._theme_btn.setToolTip(
            tr("Cycle theme: dark → high contrast → light")
        )
        self._theme_btn.clicked.connect(self._on_cycle_theme)
        self._update_btn = QPushButton(tr("Check for updates"))
        self._update_btn.setProperty("variant", "ghost")
        self._update_btn.setToolTip(
            tr(
                "Check for and download the latest version "
                "(startup checks can be configured in Settings)"
            )
        )
        self._update_btn.clicked.connect(self._on_check_update_clicked)
        self._settings_btn = QPushButton()
        self._settings_btn.setObjectName("iconBtn")
        self._settings_btn.setFixedSize(30, 30)
        self._settings_btn.setToolTip(
            tr("Configure default folders, updates, and language")
        )
        self._settings_btn.clicked.connect(self._on_open_settings)
        row.addWidget(self._theme_btn)
        row.addWidget(self._update_btn)
        row.addWidget(self._settings_btn)

        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._toolbar.addWidget(bar)
        # 保留隐藏的 _theme_combo 以兼容旧代码 / 测试引用
        self._theme_combo = QComboBox()
        self._theme_combo.addItems([S.THEME_LABELS[t] for t in S.THEMES])
        self._theme_combo.setCurrentText(S.THEME_LABELS.get(self._theme, "Dark"))
        self._theme_combo.hide()

    def _on_cycle_theme(self):
        """循环 dark → dark_hc → light → dark。"""
        order = list(S.THEMES)
        try:
            nxt = order[(order.index(self._theme) + 1) % len(order)]
        except ValueError:
            nxt = "dark"
        self._theme = nxt
        self._apply_theme(nxt)
        self._settings.set("ui.theme", nxt)
        self._settings.persist_preferences()

    def _sync_tab_pills(self, _index: int = -1):
        """客户模式隐藏导航，工程模式显示五个 Tab 并同步选中态。"""
        from satellite_debug_tool.ui import icons as _ic

        pal = S.palette(self._theme)
        engineering_active = (
            hasattr(self, "_workspace") and self._workspace.currentIndex() == 1
        )
        current_tab = self._tabs.currentIndex() if hasattr(self, "_tabs") else 0

        self._tab_pillbar.setVisible(engineering_active)
        self._engineering_mode_combo.setVisible(engineering_active)
        for tab_index, button in enumerate(self._engineering_tab_pills):
            button.setVisible(engineering_active)
            button.setChecked(engineering_active and tab_index == current_tab)

        for b in self._tab_pills:
            col = pal["accent_2"] if b.isChecked() else pal["text_2"]
            b.setIcon(_ic.icon(b.property("iconName"), color=col, size=13))

    def _select_engineering_tab(self, index: int) -> None:
        self._tabs.setCurrentIndex(index)
        # 重复点击当前页不会触发 currentChanged，需要主动恢复药丸选中态。
        self._sync_tab_pills()

    def _engineering_mode_block_reason(self, target_mode: str) -> str:
        if target_mode == ENGINEERING_SHARED_UDP:
            if self._live.is_recording():
                return tr("Stop the Engineering serial recording before switching mode")
            if self._live.session_core().device_transaction_active:
                return tr("Finish the Engineering serial device operation before switching mode")
            if self._live.is_connected():
                return tr("Disconnect the Engineering serial session before switching mode")
            return ""
        for snapshot in self._customer_devices.devices():
            if snapshot.recording_active:
                return tr("Stop shared UDP recording before switching mode")
            if snapshot.operation_busy:
                return tr("Finish the shared UDP device operation before switching mode")
        return ""

    def _on_engineering_mode_changed(self, index: int) -> None:
        if not hasattr(self, "_engineering_live_host"):
            return
        mode = str(self._engineering_mode_combo.itemData(int(index)) or "")
        if mode == self._engineering_session_mode:
            return
        reason = self._engineering_mode_block_reason(mode)
        if reason:
            blocker = QSignalBlocker(self._engineering_mode_combo)
            current = self._engineering_mode_combo.findData(
                self._engineering_session_mode
            )
            self._engineering_mode_combo.setCurrentIndex(current)
            del blocker
            self._on_status_message(reason, 5000)
            return
        self._set_engineering_session_mode(mode, persist=True)

    def _set_engineering_session_mode(
        self,
        mode: str,
        *,
        persist: bool,
    ) -> None:
        if mode not in {ENGINEERING_SERIAL, ENGINEERING_SHARED_UDP}:
            raise ValueError("unknown Engineering session mode")
        self._engineering_session_mode = mode
        for host in (
            self._engineering_live_host,
            self._engineering_device_router,
            self._engineering_tracking_router,
        ):
            host.set_mode(mode)
        index = self._engineering_mode_combo.findData(mode)
        blocker = QSignalBlocker(self._engineering_mode_combo)
        self._engineering_mode_combo.setCurrentIndex(index)
        del blocker
        if persist:
            self._settings.set("ui.engineering_session_mode", mode)
            self._settings.persist_preferences()

    def unlock_engineering_for_session(self) -> None:
        """Expose engineering diagnostics for this process without persisting it."""
        self._engineering_unlocked = True
        self._workspace.setCurrentIndex(1)
        self._sync_tab_pills()

    def _request_engineering_unlock(self) -> None:
        if self._engineering_unlocked:
            self._workspace.setCurrentIndex(
                0 if self._workspace.currentIndex() == 1 else 1
            )
            return
        answer = QMessageBox.question(
            self,
            tr("Engineering diagnostics"),
            tr(
                "Engineering diagnostics expose internal channels and device controls. "
                "Open them for this session?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.unlock_engineering_for_session()

    def unlock_production_for_session(self) -> None:
        """Expose the production console for this process without persistence."""
        self._production_unlocked = True
        self._ensure_production_workspace().activate()
        self._workspace.setCurrentIndex(2)
        self._sync_tab_pills()

    def _request_production_unlock(self) -> None:
        if (
            self._customer_bundle_factory.any_iperf_active
            or self._customer_bundle_factory.any_power_action_pending
            or self._customer_bundle_factory.confirmed_power_outputs()
        ):
            QMessageBox.warning(
                self,
                tr("Production batch test"),
                tr(
                    "Stop customer network tests and confirm all customer power outputs OFF before opening Production."
                ),
            )
            return
        if self._production_unlocked:
            self._workspace.setCurrentIndex(
                0 if self._workspace.currentIndex() == 2 else 2
            )
            return
        answer = QMessageBox.question(
            self,
            tr("Production batch test"),
            tr(
                "M19-A is an engineering-preview evidence capture workspace. It does "
                "not execute the complete formal production-release workflow. Open "
                "it for this session?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.unlock_production_for_session()

    # ============================ 主题 ============================

    @staticmethod
    def _normalize_theme(theme_raw) -> str:
        if str(theme_raw).lower() in ("dark", "light"):
            return str(theme_raw).lower()
        if theme_raw == "Dark":
            return "dark"
        if theme_raw == "Light":
            return "light"
        return S._normalize_theme(theme_raw)

    def _label_to_theme(self, label: str) -> str:
        reverse = {v: k for k, v in S.THEME_LABELS.items()}
        return reverse.get(label, S._normalize_theme(label))

    def _on_theme_changed(self, label: str):
        theme = self._label_to_theme(label)
        self._theme = theme
        self._apply_theme(theme)
        self._settings.set("ui.theme", theme)
        self._settings.persist_preferences()

    def _apply_theme(self, theme: str):
        """全局 chrome + 广播到三个 view。"""
        pal = S.palette(theme)
        # Mission Console：先把全局 QSS 注入 QApplication（chrome 控件统一底色），
        # 再让各 widget 的 per-widget setStyleSheet 覆盖局部细节
        try:
            from PySide6.QtWidgets import QApplication
            from satellite_debug_tool.ui import qss as _qss
            from satellite_debug_tool.ui import icons as _icons
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(_qss.build(theme, "small"))
            _icons.clear_cache()   # 主题切换 → 图标重新着色
        except Exception:
            pass
        self.setStyleSheet(
            f"QMainWindow {{ background-color: {pal['bg']}; color: {pal['text']}; }}"
        )
        self._style_global_bar(pal)
        # 版本号 label 也跟随主题
        if hasattr(self, "_version_label"):
            self._version_label.setStyleSheet(
                f"padding: 0 8px; color: {pal['text_muted']};"
            )
        # statusbar
        sb = self.statusBar()
        if sb is not None:
            sb.setStyleSheet(
                f"QStatusBar {{ background-color: {pal['panel']}; color: {pal['text_2']}; "
                f"border-top: 1px solid {pal['border']}; }}"
            )
        # QTabWidget pane（tab bar 已隐藏，只留内容边框）
        self._tabs.setStyleSheet(
            f"QTabWidget::pane {{ border: 0; background: {pal['bg']}; }}"
        )
        # 广播到 view
        for view in (
            self._customer,
            self._engineering_live_host,
            self._playback_host,
            self._log_host,
            self._engineering_device_router,
            self._engineering_tracking_router,
            self._production_host,
        ):
            if hasattr(view, "set_theme"):
                view.set_theme(theme, "small")
        self._customer_bundle_factory.set_theme(theme, "small")

    def _style_global_bar(self, pal: dict):
        """gbar 品牌 + 药丸 Tab + 右侧图标按钮的主题样式。"""
        from satellite_debug_tool.ui import icons as _ic

        # 顶栏容器
        self._toolbar.setStyleSheet(
            f"QToolBar {{ background-color: {pal['panel']}; border: 0; "
            f"border-bottom: 1px solid {pal['border']}; padding: 0; }}"
            f"#gbar {{ background: transparent; }}"
        )
        # 品牌方块：青色渐变 + 卫星图标
        self._brand_mark.setStyleSheet(
            f"#brandMark {{ border-radius: 7px; background: qlineargradient("
            f"x1:0,y1:0,x2:1,y2:1, stop:0 {pal['accent']}, stop:1 {pal['accent_dim']}); }}"
        )
        self._brand_mark.setPixmap(
            _ic.icon("satellite", color=pal["accent_ink"], size=15).pixmap(15, 15)
        )
        self._brand_text.setStyleSheet(
            f"#brandText {{ color: {pal['text']}; font-weight: 600; font-size: "
            f"{S.font_px(13, 'small')}px; background: transparent; }}"
        )
        # Tab 药丸容器 + 按钮
        self._tab_pillbar.setStyleSheet(
            f"#tabPills {{ background-color: {pal['panel_2']}; "
            f"border: 1px solid {pal['border']}; border-radius: 6px; }}"
            f"#tabPill {{ background: transparent; border: 0; border-radius: 4px; "
            f"color: {pal['text_2']}; padding: 4px 13px; font-size: {S.font_px(12,'small')}px; }}"
            f"#tabPill:hover {{ color: {pal['text']}; }}"
            f"#tabPill:checked {{ background-color: {pal['card_2']}; color: {pal['accent_2']}; "
            f"font-weight: 600; }}"
        )
        self._engineering_mode_combo.setStyleSheet("")
        for b in self._tab_pills:
            name = b.property("iconName")
            on = b.isChecked()
            col = pal["accent_2"] if on else pal["text_2"]
            b.setIcon(_ic.icon(name, color=col, size=13))
        # 右侧图标按钮 + ghost 检查更新
        icon_btn_qss = (
            f"#iconBtn {{ background: transparent; border: 1px solid transparent; "
            f"border-radius: 6px; }}"
            f"#iconBtn:hover {{ background-color: {pal['card_2']}; }}"
        )
        self._theme_btn.setStyleSheet(icon_btn_qss)
        self._settings_btn.setStyleSheet(icon_btn_qss)
        self._update_btn.setStyleSheet("")  # 走全局 QSS ghost variant
        theme_icon = {"dark": "moon", "dark_hc": "contrast", "light": "sun"}.get(self._theme, "moon")
        self._theme_btn.setIcon(_ic.icon(theme_icon, color=pal["text_2"], size=15))
        self._settings_btn.setIcon(_ic.icon("settings", color=pal["text_2"], size=15))
        self._update_btn.setIcon(_ic.icon("refresh", color=pal["text_2"], size=13))

    # ============================ Tab / 状态 ============================

    def _on_tab_changed(self, index: int):
        tab_id = self._TAB_IDS[index] if 0 <= index < len(self._TAB_IDS) else "live"
        self._settings.set("ui.active_tab_id", tab_id)
        self._settings.persist_preferences()
        # 切 tab 时清掉 statusbar 上残留的临时消息（不同 view 之间不串扰）
        sb = self.statusBar()
        if sb is not None:
            sb.clearMessage()
        if (
            hasattr(self, "_workspace")
            and self._workspace.currentIndex() == 1
            and self._active_engineering_tab_index != index
        ):
            self._deactivate_engineering_tab()
            self._activate_engineering_tab(index)

    def _on_workspace_changed(self, index: int) -> None:
        index = int(index)
        previous = self._active_workspace_index
        if previous == index:
            self._sync_tab_pills(index)
            return
        if previous == 0:
            deactivate_view(self._customer)
        elif previous == 1:
            self._deactivate_engineering_tab()
        elif previous == 2:
            deactivate_view(self._production_host)

        self._active_workspace_index = index
        self._sync_external_power_activity()
        if index == 0:
            activate_view(self._customer)
        elif index == 1:
            self._activate_engineering_tab(self._tabs.currentIndex())
        elif index == 2:
            activate_view(self._production_host)
        self._sync_tab_pills(index)

    def _activate_engineering_tab(self, index: int) -> None:
        if not 0 <= int(index) < self._tabs.count():
            return
        self._active_engineering_tab_index = int(index)
        activate_view(self._tabs.widget(int(index)))

    def _deactivate_engineering_tab(self) -> None:
        index = self._active_engineering_tab_index
        if index is None:
            return
        deactivate_view(self._tabs.widget(index))
        self._active_engineering_tab_index = None

    def _on_status_message(self, msg: str, timeout_ms: int):
        sb = self.statusBar()
        if sb is not None:
            sb.showMessage(msg, timeout_ms)

    def _sync_external_power_activity(self, *_args) -> None:
        customer_visible = (
            hasattr(self, "_workspace") and self._workspace.currentIndex() == 0
        )
        if hasattr(self, "_customer_bundle_factory"):
            self._customer_bundle_factory.set_customer_active(customer_visible)

    def retranslate_ui(self) -> None:
        # Transient messages arrive already formatted. Clearing one on a locale
        # change avoids leaving stale-language text without rebuilding any view.
        self.setWindowTitle(tr("SoftHertz Phased-Array Terminal Tool"))
        self._brand_text.setText(tr("SoftHertz"))
        sb = self.statusBar()
        if sb is not None:
            sb.clearMessage()
        for button, source in zip(
            self._engineering_tab_pills,
            ("Live", "Playback", "Log", "Device", "Tracking Simulator"),
        ):
            button.setText(tr(source))
        for index, source in enumerate(("Live", "Playback", "Log", "Device", "Tracking Simulator")):
            self._tabs.setTabText(index, tr(source))
        serial_index = self._engineering_mode_combo.findData(ENGINEERING_SERIAL)
        shared_index = self._engineering_mode_combo.findData(ENGINEERING_SHARED_UDP)
        self._engineering_mode_combo.setItemText(
            serial_index,
            tr("Engineering serial"),
        )
        self._engineering_mode_combo.setItemText(
            shared_index,
            tr("Shared customer UDP"),
        )
        self._engineering_mode_combo.setToolTip(
            tr("Choose an independent serial session or the selected customer UDP session")
        )
        for host in (
            self._engineering_live_host,
            self._engineering_device_router,
            self._engineering_tracking_router,
        ):
            host.retranslate_ui()
        self._customer_bundle_factory.retranslate_ui()

    # ============================ 设置 ============================

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

    def _on_open_settings(self):
        """点击 ⚙ 设置按钮：弹出路径配置弹窗。"""
        active_endpoint = (
            self._customer_devices.active_endpoint()
            if self._engineering_session_mode == ENGINEERING_SHARED_UDP
            else None
        )
        runtime = (
            None
            if active_endpoint is None
            else self._customer_devices.runtime(active_endpoint)
        )
        profile_store = (
            runtime.core.profile_store
            if runtime is not None
            else getattr(self._live, "_profile_store", None)
        )
        workspace_index = self._workspace.currentIndex()
        scope = {
            0: SETTINGS_SCOPE_CUSTOMER,
            1: SETTINGS_SCOPE_ENGINEERING,
            2: SETTINGS_SCOPE_PRODUCTION,
        }.get(workspace_index, SETTINGS_SCOPE_CUSTOMER)
        dlg = SettingsDialog(
            self._settings,
            self,
            profile_store=profile_store,
            device_udp_port_editable=lambda: not self._udp_broker.has_active_demand,
            scope=scope,
        )
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        if accepted and scope == SETTINGS_SCOPE_CUSTOMER:
            self._sync_external_power_activity()
        if self._production is not None:
            self._production.refresh_production_configurations()

    # ============================ M11 更新 ============================

    def _on_check_update_clicked(self):
        """点击 🔄 检查更新：手动触发，弹 UpdateDialog 立即开始检查。"""
        dlg = UpdateDialog(self._settings, parent=self, auto_start=True)
        dlg.exec()

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

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._production is not None and not self._production.confirm_shutdown():
            event.ignore()
            return
        active_outputs = self._customer_bundle_factory.confirmed_power_outputs()
        if active_outputs:
            targets = ", ".join(f"{ip}:{port}" for ip, port in active_outputs)
            answer = QMessageBox.question(
                self,
                tr("External power"),
                tr(
                    "Customer power output is ON for {targets}. Close the outputs before exiting?",
                    targets=targets,
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if (
                answer == QMessageBox.StandardButton.Yes
                and not self._customer_bundle_factory.disable_confirmed_outputs()
            ):
                QMessageBox.warning(
                    self,
                    tr("External power"),
                    tr("One or more customer power outputs could not be confirmed OFF."),
                )
                event.ignore()
                return
        if self._customer_bundle_factory.any_iperf_active:
            answer = QMessageBox.question(
                self,
                tr("Network test in progress"),
                tr("Stop the active iperf3 test and exit?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if not self._shutdown_background_update_check():
            QMessageBox.warning(
                self,
                tr("Check for updates"),
                tr("Background update check did not stop cleanly"),
            )
            event.ignore()
            return
        # 工程串口 LiveView 独占自己的 worker。必须先确认线程已停止，再进入
        # customer/directory 的不可逆释放阶段；否则超时后 event.ignore() 会留下
        # 一个已被部分拆除、但窗口仍存活的进程组合。
        if not self._live.shutdown():
            self._sync_external_power_activity()
            QMessageBox.warning(
                self,
                tr("Device operation in progress"),
                tr("The engineering transport did not stop cleanly"),
            )
            event.ignore()
            return
        if not self._customer.shutdown():
            self._sync_external_power_activity()
            QMessageBox.warning(
                self,
                tr("Device operation in progress"),
                tr(
                    "Customer session shutdown did not complete; finish the active "
                    "local operation and retry"
                ),
            )
            event.ignore()
            return
        active = self._active_workspace_index
        if active == 0:
            deactivate_view(self._customer)
        elif active == 1:
            self._deactivate_engineering_tab()
        elif active == 2:
            deactivate_view(self._production_host)
        self._active_workspace_index = None
        self._playback_host.shutdown()
        self._log_host.shutdown()
        self._device_host.shutdown()
        self._tracking_simulator_host.shutdown()
        self._production_host.shutdown()
        try:
            self._customer_devices.shutdown()
        except CustomerDeviceDirectoryError as exc:
            QMessageBox.warning(self, tr("Customer devices"), str(exc))
            event.ignore()
            return
        if not self._endpoint_directory.shutdown():
            QMessageBox.warning(
                self,
                tr("Device operation in progress"),
                tr("Shared device sessions still have active owners"),
            )
            event.ignore()
            return
        if not self._udp_broker.shutdown():
            QMessageBox.warning(
                self,
                tr("Device operation in progress"),
                tr("The shared UDP transport did not stop cleanly"),
            )
            event.ignore()
            return
        super().closeEvent(event)
