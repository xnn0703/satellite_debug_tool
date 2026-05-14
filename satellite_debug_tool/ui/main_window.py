"""MainWindow —— 顶层窗口，QTabWidget 容器。

M7-S4 重构：原 1189 行的 MainWindow 业务主体已抽到 LiveView。此处只保留：

- 顶部全局工具栏：主题切换 combo
- 中部 QTabWidget：实时 / 回放 / Log 三个 view
- 底部 QStatusBar：接所有 view 的 status_message 信号

主题切换由 MainWindow 统一广播到三个 view。各 view 间数据互不影响（独立
DataStore / ProfileStore）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QMainWindow,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QWidget,
)

from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.live_view import LiveView
from satellite_debug_tool.ui.log_view import LogView
from satellite_debug_tool.ui.playback_view import PlaybackView


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

        # ---------- 顶部全局 toolbar（主题切换 + 活动 tab 标识占位） ----------
        self._toolbar = QToolBar()
        self._toolbar.setMovable(False)
        self._toolbar.setFixedHeight(32)
        self.addToolBar(self._toolbar)

        self._theme_combo = QComboBox()
        self._theme_combo.addItems([S.THEME_LABELS[t] for t in S.THEMES])
        self._theme_combo.setFixedWidth(110)
        self._theme_combo.setCurrentText(S.THEME_LABELS.get(self._theme, "深色"))
        self._theme_combo.setToolTip(
            "深色:桌面开发 / 深色·高对比:车载强光屏 500cd/m² / 浅色:日间外场"
        )
        self._theme_combo.currentTextChanged.connect(self._on_theme_changed)
        self._toolbar.addWidget(QLabel("主题:"))
        self._toolbar.addWidget(self._theme_combo)

        # spacer 让后续元素靠右（暂无元素，预留扩展）
        spacer = QWidget()
        from PySide6.QtWidgets import QSizePolicy
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._toolbar.addWidget(spacer)

        # ---------- 中部 QTabWidget ----------
        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.North)
        self._live = LiveView(settings=self._settings)
        self._playback = PlaybackView()
        self._log = LogView()
        self._tabs.addTab(self._live, "实时")
        self._tabs.addTab(self._playback, "回放")
        self._tabs.addTab(self._log, "Log")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        for view in (self._live, self._playback, self._log):
            view.status_message.connect(self._on_status_message)

        self.setCentralWidget(self._tabs)

        # ---------- 底部 statusbar ----------
        self.setStatusBar(QStatusBar())

        # ---------- 应用主题 + 恢复 active tab ----------
        self._apply_theme(self._theme)
        active = self._settings.get("ui.active_tab", "实时")
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == active:
                self._tabs.setCurrentIndex(i)
                break

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
        self.setStyleSheet(
            f"QMainWindow {{ background-color: {pal['bg']}; color: {pal['text']}; }}"
        )
        self._toolbar.setStyleSheet(
            f"background-color: {pal['panel']}; border: none; padding: 2px 4px;"
        )
        self._theme_combo.setStyleSheet(
            f"background-color: {pal['input_bg']}; color: {pal['text']}; "
            f"border: 1px solid {pal['input_border']}; padding: 2px 6px; border-radius: 2px;"
        )
        # toolbar 上 QLabel "主题:" 也要刷新
        for lbl in self._toolbar.findChildren(QLabel):
            lbl.setStyleSheet(f"color: {pal['text']}; background: transparent;")
        # statusbar
        sb = self.statusBar()
        if sb is not None:
            sb.setStyleSheet(f"background-color: {pal['panel']}; color: {pal['text']};")
        # QTabWidget 标签条
        self._tabs.setStyleSheet(
            f"QTabBar::tab {{ background: {pal['panel']}; color: {pal['text_muted']}; "
            f"padding: 6px 16px; border: 1px solid {pal['border']}; }}"
            f"QTabBar::tab:selected {{ background: {pal['bg']}; color: {pal['text']}; "
            f"border-bottom: 2px solid {pal['primary']}; }}"
            f"QTabWidget::pane {{ border: 1px solid {pal['border']}; "
            f"background: {pal['bg']}; }}"
        )
        # 广播到 view
        for view in (self._live, self._playback, self._log):
            if hasattr(view, "set_theme"):
                view.set_theme(theme, "small")

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
