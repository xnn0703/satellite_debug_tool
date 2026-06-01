"""MainWindow —— 顶层窗口，QTabWidget 容器。

M7-S4 重构：原 1189 行的 MainWindow 业务主体已抽到 LiveView。此处只保留：

- 顶部全局工具栏：主题切换 combo
- 中部 QTabWidget：实时 / 回放 / Log 三个 view
- 底部 QStatusBar：接所有 view 的 status_message 信号

主题切换由 MainWindow 统一广播到三个 view。各 view 间数据互不影响（独立
DataStore / ProfileStore）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QWidget,
)

from satellite_debug_tool import __version__
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.device_view import DeviceView
from satellite_debug_tool.ui.live_view import LiveView
from satellite_debug_tool.ui.log_view import LogView
from satellite_debug_tool.ui.playback_view import PlaybackView
from satellite_debug_tool.ui.settings_dialog import SettingsDialog
from satellite_debug_tool.ui.update_dialog import (
    UpdateDialog,
    silent_background_check,
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Satellite Debug Tool")
        self.resize(1280, 800)
        self.setMinimumWidth(1024)
        self.setMinimumHeight(600)

        self._settings = Settings()
        # M7：一次性清掉历史遗留 settings key（attitude 已迁移到全自动；
        # ui.font_scale 已固化为 small，UI 不再暴露调节入口）
        cleaned = False
        if self._settings.remove("attitude"):
            cleaned = True
        if self._settings.remove("ui.font_scale"):
            cleaned = True
        if cleaned:
            self._settings.save()

        # 主题：兼容旧 "Dark"/"Light" 大写写法
        theme_raw = self._settings.get("ui.theme", "dark")
        self._theme = self._normalize_theme(theme_raw)

        # ---------- 顶部全局 gbar（品牌 + 居中 Tab 药丸 + 右侧控件，Mission Console） ----------
        self._build_global_bar()

        # ---------- 中部 QTabWidget（原生 tab bar 隐藏，由药丸驱动） ----------
        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.North)
        self._tabs.tabBar().hide()   # gbar 药丸接管 tab 切换
        self._tabs.setDocumentMode(True)
        self._live = LiveView(settings=self._settings)
        self._playback = PlaybackView(settings=self._settings)
        self._log = LogView(settings=self._settings)
        self._device = DeviceView(settings=self._settings)
        self._tabs.addTab(self._live, "实时")
        self._tabs.addTab(self._playback, "回放")
        self._tabs.addTab(self._log, "Log")
        self._tabs.addTab(self._device, "设备")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._tabs.currentChanged.connect(self._sync_tab_pills)

        # M9: 连接共享 — Live Tab 的 worker 和帧数据广播给 Device Tab
        self._live.connected_worker_changed.connect(self._device.set_worker)
        self._live.frame_received.connect(self._device._on_frame_received)

        for view in (self._live, self._playback, self._log, self._device):
            view.status_message.connect(self._on_status_message)

        self.setCentralWidget(self._tabs)

        # ---------- 底部 statusbar ----------
        self.setStatusBar(QStatusBar())
        # M11：状态栏右下角显示当前版本号
        self._version_label = QLabel(f"v{__version__}")
        self._version_label.setStyleSheet("padding: 0 8px; color: #888;")
        self.statusBar().addPermanentWidget(self._version_label)

        # ---------- 应用主题 + 恢复 active tab ----------
        self._apply_theme(self._theme)
        active = self._settings.get("ui.active_tab", "实时")
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == active:
                self._tabs.setCurrentIndex(i)
                break

        # M11：启动后台静默检查更新（settings.update.auto_check 控制）
        self._bg_check_thread = None
        QTimer.singleShot(2000, self._kick_silent_update_check)

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
        self._brand_text = QLabel("Satellite Debug Tool")
        self._brand_text.setObjectName("brandText")
        row.addWidget(self._brand_mark)
        row.addWidget(self._brand_text)
        row.addStretch(1)

        # 中：Tab 药丸（seg--accent）
        self._tab_pillbar = QWidget()
        self._tab_pillbar.setObjectName("tabPills")
        pill_row = QHBoxLayout(self._tab_pillbar)
        pill_row.setContentsMargins(3, 3, 3, 3)
        pill_row.setSpacing(2)
        self._tab_pills: list[QPushButton] = []
        pill_defs = [("实时", "activity"), ("回放", "history"), ("Log", "list"), ("设备", "cpu")]
        for idx, (label, icon_name) in enumerate(pill_defs):
            b = QPushButton(label)
            b.setObjectName("tabPill")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setProperty("iconName", icon_name)
            b.clicked.connect(lambda _checked, i=idx: self._tabs.setCurrentIndex(i))
            pill_row.addWidget(b)
            self._tab_pills.append(b)
        row.addWidget(self._tab_pillbar)
        row.addStretch(1)

        # 右：主题切换 + 检查更新 + 设置（图标按钮）
        self._theme_btn = QPushButton()
        self._theme_btn.setObjectName("iconBtn")
        self._theme_btn.setFixedSize(30, 30)
        self._theme_btn.setToolTip("循环切换主题：深色 → 强光 → 浅色")
        self._theme_btn.clicked.connect(self._on_cycle_theme)
        self._update_btn = QPushButton("检查更新")
        self._update_btn.setProperty("variant", "ghost")
        self._update_btn.setToolTip("手动检查并下载最新版本（也可在设置中开关启动自检）")
        self._update_btn.clicked.connect(self._on_check_update_clicked)
        self._settings_btn = QPushButton()
        self._settings_btn.setObjectName("iconBtn")
        self._settings_btn.setFixedSize(30, 30)
        self._settings_btn.setToolTip("配置文件保存 / 加载的默认目录 / 更新设置")
        self._settings_btn.clicked.connect(self._on_open_settings)
        row.addWidget(self._theme_btn)
        row.addWidget(self._update_btn)
        row.addWidget(self._settings_btn)

        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._toolbar.addWidget(bar)
        # 保留隐藏的 _theme_combo 以兼容旧代码 / 测试引用
        self._theme_combo = QComboBox()
        self._theme_combo.addItems([S.THEME_LABELS[t] for t in S.THEMES])
        self._theme_combo.setCurrentText(S.THEME_LABELS.get(self._theme, "深色"))
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
        self._settings.save()

    def _sync_tab_pills(self, index: int):
        """QTabWidget 切换 → 同步药丸选中态 + 图标着色。"""
        from satellite_debug_tool.ui import icons as _ic
        pal = S.palette(self._theme)
        for i, b in enumerate(getattr(self, "_tab_pills", [])):
            b.setChecked(i == index)
            col = pal["accent_2"] if i == index else pal["text_2"]
            b.setIcon(_ic.icon(b.property("iconName"), color=col, size=13))

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
        self._settings.save()

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
        for view in (self._live, self._playback, self._log, self._device):
            if hasattr(view, "set_theme"):
                view.set_theme(theme, "small")

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
        name = self._tabs.tabText(index)
        self._settings.set("ui.active_tab", name)
        self._settings.save()
        # 切 tab 时清掉 statusbar 上残留的临时消息（不同 view 之间不串扰）
        sb = self.statusBar()
        if sb is not None:
            sb.clearMessage()

    def _on_status_message(self, msg: str, timeout_ms: int):
        sb = self.statusBar()
        if sb is not None:
            sb.showMessage(msg, timeout_ms)

    # ============================ 设置 ============================

    def _on_open_settings(self):
        """点击 ⚙ 设置按钮：弹出路径配置弹窗。"""
        # 把当前 LiveView 的 profile_store 传给设置弹窗，让"管理图表分组"按钮能用
        profile_store = getattr(self._live, "_profile_store", None)
        dlg = SettingsDialog(self._settings, self, profile_store=profile_store)
        dlg.exec()

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

    def _on_silent_check_found_new(self, latest):
        """后台检查发现新版：状态栏提示，用户点击 / 工具栏按钮可展开 UpdateDialog。"""
        msg = (
            f"🆕 发现新版本 {latest.tag_name}（当前 v{__version__}）— 点工具栏「检查更新」更新"
        )
        sb = self.statusBar()
        if sb is not None:
            sb.showMessage(msg, 15000)
