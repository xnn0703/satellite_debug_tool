"""UpdateDialog — 检查/下载/应用升级的三阶段 GUI（M11 P7）。

页面（QStackedWidget）：
  CHECKING  ：正在查 GitHub Release API
  UP_TO_DATE：已是最新版
  NEW_FOUND ：发现新版（含 release notes + 立即更新 / 跳过 / 稍后）
  DOWNLOADING：下载中（进度条 + 取消按钮）
  LAUNCHING ：准备启动 updater 并退出主程序
  ERROR     ：失败页（含错误信息 + 重试 / 关闭）

线程：
  CheckWorker  → 后台 fetch_latest（QObject 配 QThread）
  DownloadWorker → 后台 download_all + 进度信号
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool import __version__ as CURRENT_VERSION
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.i18n import register_translatable, set_translatable_text, tr
from satellite_debug_tool.release_config import load_release_config
from satellite_debug_tool.updater.checker import (
    LatestRelease,
    ReleaseChecker,
    UpdateCheckError,
    compare_versions,
    current_platform,
)
from satellite_debug_tool.updater.downloader import (
    DownloadCancelled,
    DownloadError,
    Downloader,
)
from satellite_debug_tool.updater.errors import UpdaterError


log = logging.getLogger(__name__)


# ============================ Worker（在 QThread 里跑） ============================


class _CheckWorker(QObject):
    finished = Signal(object)   # LatestRelease
    failed = Signal(object)      # UpdaterError

    def __init__(self, checker: ReleaseChecker):
        super().__init__()
        self._checker = checker

    def run(self) -> None:
        try:
            result = self._checker.fetch_latest()
            self.finished.emit(result)
        except UpdateCheckError as e:
            self.failed.emit(e)
        except Exception as e:    # 保险：任何其他异常都不让 Worker 崩溃
            self.failed.emit(UpdaterError("unexpected_error", str(e)))


class _DownloadWorker(QObject):
    progress = Signal(int, int)        # (bytes_done, bytes_total)
    finished = Signal(list)            # List[Path]
    failed = Signal(object)
    cancelled = Signal()

    def __init__(self, downloader: Downloader, assets: list, dest_dir: Path):
        super().__init__()
        self._downloader = downloader
        self._assets = assets
        self._dest = dest_dir
        self._cancel = None             # 由 dialog 注入 threading.Event

    def attach_cancel_event(self, ev) -> None:
        self._cancel = ev

    def run(self) -> None:
        try:
            paths = self._downloader.download_all(
                self._assets, self._dest,
                on_progress=lambda d, t: self.progress.emit(d, t),
                cancel_event=self._cancel,
            )
            self.finished.emit(paths)
        except DownloadCancelled:
            self.cancelled.emit()
        except DownloadError as e:
            self.failed.emit(e)
        except Exception as e:
            self.failed.emit(UpdaterError("unexpected_error", str(e)))


# ============================ 主对话框 ============================


# 页面常量
_PAGE_CHECKING = 0
_PAGE_UP_TO_DATE = 1
_PAGE_NEW_FOUND = 2
_PAGE_DOWNLOADING = 3
_PAGE_LAUNCHING = 4
_PAGE_ERROR = 5


def _updater_error_text(error: UpdaterError) -> str:
    code = error.code
    context = error.context
    detail = error.detail
    if code == "check_http_error":
        return tr(
            "Release server returned HTTP {status}: {detail}",
            status=context.get("status", "—"),
            detail=detail,
        )
    if code == "check_network_error":
        return tr("Network error: {detail}", detail=detail)
    if code == "check_response_parse_error":
        return tr("Could not parse the release-server response: {detail}", detail=detail)
    if code == "check_response_format_error":
        return tr("Unexpected release-server response: {detail}", detail=detail)
    if code == "download_no_assets":
        return tr("The release contains no downloadable assets")
    if code == "download_destination_missing":
        return tr(
            "Download destination does not exist: {path}",
            path=context.get("path", "—"),
        )
    if code == "download_cancelled":
        return tr("Download cancelled")
    if code == "download_size_mismatch":
        return tr(
            "{asset}: size mismatch (expected {expected}, got {actual})",
            asset=context.get("asset", "—"),
            expected=context.get("expected", "—"),
            actual=context.get("actual", "—"),
        )
    if code == "download_http_error":
        return tr(
            "{asset}: HTTP {status}",
            asset=context.get("asset", "—"),
            status=context.get("status", "—"),
        )
    if code == "download_retries_exhausted":
        return tr(
            "{asset}: failed after {attempts} attempt(s): {detail}",
            asset=context.get("asset", "—"),
            attempts=context.get("attempts", "—"),
            detail=detail,
        )
    if code == "unexpected_error":
        return tr("Unexpected error: {detail}", detail=detail)
    return tr(
        "{code}: {detail}",
        code=code,
        detail=detail or tr("No additional details"),
    )


class UpdateDialog(QDialog):
    """检查/下载/应用三阶段升级弹窗。

    Args:
        settings: 全局 Settings；用来记录 last_check_iso / skip_version
        parent: 父 widget
        auto_start: True 时弹窗即开始检查（手动按钮触发用）
    """

    def __init__(
        self,
        settings: Settings,
        parent: Optional[QWidget] = None,
        auto_start: bool = True,
    ):
        super().__init__(parent)
        self._settings = settings
        self._release_cfg = load_release_config()

        self._latest: Optional[LatestRelease] = None
        self._download_dir: Optional[Path] = None

        # 线程对象（每次操作 new 一次）
        self._check_thread: Optional[QThread] = None
        self._check_worker: Optional[_CheckWorker] = None
        self._download_thread: Optional[QThread] = None
        self._download_worker: Optional[_DownloadWorker] = None
        self._cancel_event = None
        self._last_error_operation = ""
        self._last_error: Optional[UpdaterError] = None

        self.setWindowTitle(tr("Check for updates"))
        self.setMinimumSize(520, 360)
        self._build_ui()
        register_translatable(self)

        if auto_start:
            QTimer.singleShot(0, self.start_check)

    # ---------- UI 构造 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # 顶部：当前版本号
        self._header = QLabel(tr("Current version: v{version}", version=CURRENT_VERSION))
        self._header.setStyleSheet("font-weight: bold;")
        root.addWidget(self._header)

        self._stack = QStackedWidget()
        root.addWidget(self._stack, 1)

        # 页 0：检查中
        page_checking = QWidget()
        l0 = QVBoxLayout(page_checking)
        l0.addStretch(1)
        lbl = QLabel(tr("Connecting to the release server..."))
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l0.addWidget(lbl)
        l0.addStretch(1)
        self._stack.addWidget(page_checking)

        # 页 1：已是最新
        page_uptodate = QWidget()
        l1 = QVBoxLayout(page_uptodate)
        l1.addStretch(1)
        msg = QLabel(tr("✅ You are using the latest version."))
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setStyleSheet("font-size: 14px;")
        l1.addWidget(msg)
        l1.addStretch(1)
        btn_row1 = QHBoxLayout()
        btn_row1.addStretch(1)
        b1 = QPushButton(tr("Close"))
        b1.clicked.connect(self.accept)
        btn_row1.addWidget(b1)
        l1.addLayout(btn_row1)
        self._stack.addWidget(page_uptodate)

        # 页 2：发现新版
        page_new = QWidget()
        l2 = QVBoxLayout(page_new)
        self._lbl_new_version = QLabel()
        self._lbl_new_version.setStyleSheet("font-size: 14px; font-weight: bold;")
        l2.addWidget(self._lbl_new_version)
        l2.addWidget(QLabel(tr("Release notes:")))
        self._txt_release_notes = QTextEdit()
        self._txt_release_notes.setReadOnly(True)
        l2.addWidget(self._txt_release_notes, 1)
        btn_row2 = QHBoxLayout()
        self._btn_skip = QPushButton(tr("Skip this version"))
        self._btn_skip.setToolTip(
            tr("Do not remind me about this version; reset this in Settings")
        )
        self._btn_skip.clicked.connect(self._on_skip_version)
        self._btn_later = QPushButton(tr("Later"))
        self._btn_later.clicked.connect(self.reject)
        self._btn_upgrade = QPushButton(tr("Update now"))
        self._btn_upgrade.setDefault(True)
        self._btn_upgrade.setStyleSheet("font-weight: bold;")
        self._btn_upgrade.clicked.connect(self._on_start_download)
        btn_row2.addWidget(self._btn_skip)
        btn_row2.addStretch(1)
        btn_row2.addWidget(self._btn_later)
        btn_row2.addWidget(self._btn_upgrade)
        l2.addLayout(btn_row2)
        self._stack.addWidget(page_new)

        # 页 3：下载中
        page_dl = QWidget()
        l3 = QVBoxLayout(page_dl)
        self._lbl_dl_title = QLabel(tr("Downloading..."))
        l3.addWidget(self._lbl_dl_title)
        self._dl_progress = QProgressBar()
        self._dl_progress.setRange(0, 100)
        l3.addWidget(self._dl_progress)
        self._lbl_dl_bytes = QLabel("0 / 0 MB")
        l3.addWidget(self._lbl_dl_bytes)
        l3.addStretch(1)
        btn_row3 = QHBoxLayout()
        btn_row3.addStretch(1)
        self._btn_dl_cancel = QPushButton(tr("Cancel"))
        self._btn_dl_cancel.clicked.connect(self._on_cancel_download)
        btn_row3.addWidget(self._btn_dl_cancel)
        l3.addLayout(btn_row3)
        self._stack.addWidget(page_dl)

        # 页 4：准备启动 updater
        page_launch = QWidget()
        l4 = QVBoxLayout(page_launch)
        l4.addStretch(1)
        lbl4 = QLabel(tr("Starting the updater; the application will exit..."))
        lbl4.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl4.setStyleSheet("font-size: 14px;")
        l4.addWidget(lbl4)
        self._lbl_launch_detail = QLabel(
            tr("(the new version will start automatically after the update)")
        )
        self._lbl_launch_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_launch_detail.setStyleSheet("color: #888;")
        l4.addWidget(self._lbl_launch_detail)
        l4.addStretch(1)
        self._stack.addWidget(page_launch)

        # 页 5：错误
        page_err = QWidget()
        l5 = QVBoxLayout(page_err)
        l5.addStretch(1)
        self._lbl_err_title = QLabel(tr("❌ Update error"))
        self._lbl_err_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_err_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #d33;")
        l5.addWidget(self._lbl_err_title)
        self._lbl_err_detail = QLabel()
        self._lbl_err_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_err_detail.setWordWrap(True)
        l5.addWidget(self._lbl_err_detail)
        l5.addStretch(1)
        btn_row5 = QHBoxLayout()
        btn_row5.addStretch(1)
        self._btn_err_retry = QPushButton(tr("Retry"))
        self._btn_err_retry.clicked.connect(self.start_check)
        self._btn_err_close = QPushButton(tr("Close"))
        self._btn_err_close.clicked.connect(self.reject)
        btn_row5.addWidget(self._btn_err_retry)
        btn_row5.addWidget(self._btn_err_close)
        l5.addLayout(btn_row5)
        self._stack.addWidget(page_err)

        self._stack.setCurrentIndex(_PAGE_CHECKING)

    # ---------- 流程 ----------

    def start_check(self) -> None:
        """开始检查更新；可重复调用（重试 / 手动按钮）。"""
        self._cleanup_check_thread()
        self._stack.setCurrentIndex(_PAGE_CHECKING)
        cfg = self._release_cfg
        checker = ReleaseChecker(
            owner=cfg["github_owner"],
            repo=cfg["github_repo"],
            api_base=cfg["github_api"],
        )
        self._check_thread = QThread(self)
        self._check_worker = _CheckWorker(checker)
        self._check_worker.moveToThread(self._check_thread)
        self._check_thread.started.connect(self._check_worker.run)
        self._check_worker.finished.connect(self._on_check_done)
        self._check_worker.failed.connect(self._on_check_failed)
        self._check_thread.start()

        # 记录检查时间
        self._settings.set(
            "update.last_check_iso",
            datetime.now(timezone.utc).isoformat(),
        )
        self._settings.save()

    def _on_check_done(self, latest: LatestRelease) -> None:
        self._cleanup_check_thread()
        self._latest = latest

        cmp = compare_versions(CURRENT_VERSION, latest.tag_name)
        if cmp >= 0:
            self._stack.setCurrentIndex(_PAGE_UP_TO_DATE)
            return

        # 有更新可用
        platform = current_platform()
        assets = latest.assets_for_platform(platform)
        if not assets:
            self._show_error(
                "Version {version} has no {platform} package.\n"
                "Try again later or download it in a browser: {url}",
                version=latest.tag_name,
                platform=platform,
                url=latest.html_url,
            )
            return

        set_translatable_text(
            "🆕 Version {latest} is available (current v{current})",
            self._lbl_new_version,
            latest=latest.tag_name,
            current=CURRENT_VERSION,
        )
        notes = latest.body[:2000] if latest.body else tr("(no release notes)")
        self._txt_release_notes.setPlainText(notes)
        self._stack.setCurrentIndex(_PAGE_NEW_FOUND)

    def _on_check_failed(self, error: UpdaterError) -> None:
        self._cleanup_check_thread()
        self._show_updater_error("check", error)

    def _on_skip_version(self) -> None:
        if self._latest is None:
            self.reject()
            return
        self._settings.set("update.skip_version", self._latest.tag_name)
        self._settings.save()
        self.reject()

    # ---------- 下载 ----------

    def _on_start_download(self) -> None:
        if self._latest is None:
            return
        assets = self._latest.assets_for_platform(current_platform())
        if not assets:
            self._show_error("Release package is missing; try again later")
            return

        # 下载目录：~/.satellite_debug_tool/updates/<tag>/
        from pathlib import Path as _P
        base = _P.home() / ".satellite_debug_tool" / "updates" / self._latest.tag_name
        base.mkdir(parents=True, exist_ok=True)
        self._download_dir = base

        self._dl_progress.setValue(0)
        total_mb = sum(a.size for a in assets) / 1024 / 1024
        self._lbl_dl_bytes.setText(f"0.00 / {total_mb:.2f} MB")
        set_translatable_text(
            "Downloading update package ({version})...",
            self._lbl_dl_title,
            version=self._latest.tag_name,
        )
        self._stack.setCurrentIndex(_PAGE_DOWNLOADING)

        from threading import Event
        self._cancel_event = Event()
        downloader = Downloader()
        self._download_thread = QThread(self)
        self._download_worker = _DownloadWorker(downloader, assets, base)
        self._download_worker.attach_cancel_event(self._cancel_event)
        self._download_worker.moveToThread(self._download_thread)
        self._download_thread.started.connect(self._download_worker.run)
        self._download_worker.progress.connect(self._on_dl_progress)
        self._download_worker.finished.connect(self._on_dl_finished)
        self._download_worker.failed.connect(self._on_dl_failed)
        self._download_worker.cancelled.connect(self._on_dl_cancelled)
        self._download_thread.start()

    def _on_dl_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._dl_progress.setValue(int(done * 100 / total))
            mb = 1024 * 1024
            self._lbl_dl_bytes.setText(f"{done/mb:.2f} / {total/mb:.2f} MB")
        else:
            self._dl_progress.setRange(0, 0)   # 不确定进度

    def _on_dl_finished(self, paths: List[Path]) -> None:
        self._cleanup_download_thread()
        self._launch_updater(paths)

    def _on_dl_failed(self, error: UpdaterError) -> None:
        self._cleanup_download_thread()
        self._show_updater_error("download", error)

    def _on_dl_cancelled(self) -> None:
        self._cleanup_download_thread()
        # 回退到"新版可用"页让用户选择
        if self._latest is not None:
            self._stack.setCurrentIndex(_PAGE_NEW_FOUND)
        else:
            self.reject()

    def _on_cancel_download(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._btn_dl_cancel.setEnabled(False)
        set_translatable_text("Cancelling...", self._lbl_dl_title)

    # ---------- 启动 updater ----------

    def _launch_updater(self, volumes: List[Path]) -> None:
        """启动 updater 子进程，主程序退出。"""
        self._stack.setCurrentIndex(_PAGE_LAUNCHING)
        QTimer.singleShot(100, lambda: self._do_launch_updater(volumes))

    def _do_launch_updater(self, volumes: List[Path]) -> None:
        # 找 install_dir + updater 可执行
        install_dir = _detect_install_dir()
        updater_exe = _detect_updater_exe()
        if install_dir is None or updater_exe is None:
            self._show_error(
                "The install directory or updater executable was not found.\n"
                "Use a PyInstaller package or download and extract the archive manually.\n"
                "install_dir={install_dir}, updater={updater}",
                install_dir=install_dir,
                updater=updater_exe,
            )
            return

        workdir = Path(tempfile.mkdtemp(prefix="satdbg_update_"))
        log_path = workdir / "updater.log"

        argv = [
            str(updater_exe),
            "--pid", str(os.getpid()),
            "--install-dir", str(install_dir),
            "--workdir", str(workdir),
            "--log", str(log_path),
            "--volumes", *(str(v) for v in volumes),
        ]
        log.info("Starting updater: %s", argv)
        try:
            if sys.platform.startswith("win"):
                # DETACHED_PROCESS = 0x08
                subprocess.Popen(argv, creationflags=0x00000008, close_fds=True)
            else:
                subprocess.Popen(argv, start_new_session=True, close_fds=True)
        except OSError as e:
            self._show_error("Failed to start updater: {detail}", detail=e)
            return

        set_translatable_text(
            "✅ Updater started; the application will exit...",
            self._lbl_launch_detail,
        )
        # 留一点点时间显示提示，再退出
        QTimer.singleShot(500, self._exit_app)

    def _exit_app(self) -> None:
        # 优雅退出：先关弹窗，让 main loop quit
        self.accept()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()
        # 兜底
        QTimer.singleShot(200, lambda: sys.exit(0))

    # ---------- 错误 / 清理 ----------

    def _show_error(self, source: str, **values) -> None:
        self._last_error_operation = ""
        self._last_error = None
        set_translatable_text(
            source,
            self._lbl_err_detail,
            **values,
        )
        self._stack.setCurrentIndex(_PAGE_ERROR)

    def _show_updater_error(self, operation: str, error: UpdaterError) -> None:
        self._last_error_operation = operation
        self._last_error = error
        detail = _updater_error_text(error)
        if operation == "check":
            set_translatable_text(
                "Update check failed: {detail}",
                self._lbl_err_detail,
                detail=detail,
            )
        else:
            set_translatable_text(
                "Download failed: {detail}",
                self._lbl_err_detail,
                detail=detail,
            )
        self._stack.setCurrentIndex(_PAGE_ERROR)

    def retranslate_ui(self) -> None:
        set_translatable_text(
            "Current version: v{version}",
            self._header,
            version=CURRENT_VERSION,
        )
        if self._last_error is not None:
            self._show_updater_error(self._last_error_operation, self._last_error)

    def _cleanup_check_thread(self) -> None:
        if self._check_thread is not None:
            self._check_thread.quit()
            self._check_thread.wait(2000)
        self._check_thread = None
        self._check_worker = None

    def _cleanup_download_thread(self) -> None:
        if self._download_thread is not None:
            self._download_thread.quit()
            self._download_thread.wait(2000)
        self._download_thread = None
        self._download_worker = None

    def closeEvent(self, event) -> None:   # noqa: N802 (Qt API)
        self._cleanup_check_thread()
        self._cleanup_download_thread()
        super().closeEvent(event)


# ============================ 路径探测辅助 ============================


def _detect_install_dir() -> Optional[Path]:
    """探测主程序的安装目录（mac .app / win onedir 父目录）。

    开发模式（python -m satellite_debug_tool.main）→ None
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    # mac .app：从 .../MyApp.app/Contents/MacOS/SatelliteDebugTool 找回 .app
    for parent in [exe.parent, *exe.parents]:
        if parent.suffix == ".app":
            return parent
    # win onedir：可执行同级目录
    return exe.parent


def _detect_updater_exe() -> Optional[Path]:
    """探测 updater 可执行。约定与主程序同级（PyInstaller MERGE 共享）。"""
    if not getattr(sys, "frozen", False):
        # 开发模式：用 python -m satellite_debug_tool.updater.main
        # 但这种 path 不能直接传给 Popen，所以开发模式不支持自动升级
        return None
    install = _detect_install_dir()
    if install is None:
        return None
    # mac：.../SatelliteDebugTool.app/Contents/MacOS/updater
    if install.suffix == ".app":
        macos_dir = install / "Contents" / "MacOS"
        candidate = macos_dir / "updater"
        if candidate.exists():
            return candidate
    # win / linux onedir：install_dir/updater(.exe)
    for name in ("updater.exe", "updater"):
        c = install / name
        if c.exists():
            return c
    return None


# ============================ 后台静默检查 ============================


def should_check_in_background(settings: Settings) -> bool:
    """根据 settings.update 配置判断是否该启动后台静默检查。

    `SATELLITE_UPDATE_CHECK=0` 让测试 / CI 使用明确的关闭状态。
    """
    environment_state = os.environ.get("SATELLITE_UPDATE_CHECK", "1").strip().lower()
    if environment_state in {"0", "false", "off", "no"}:
        return False
    if not settings.get("update.auto_check", True):
        return False
    last_iso = settings.get("update.last_check_iso", "")
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    interval_hours = float(settings.get("update.check_interval_hours", 24))
    elapsed_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return elapsed_h >= interval_hours


def silent_background_check(
    settings: Settings,
    parent: Optional[QWidget] = None,
    on_new_version: Optional[callable] = None,
) -> Optional[QThread]:
    """启动一次后台 check（不弹任何 GUI）；发现新版调 on_new_version(LatestRelease)。

    跳过条件：auto_check=False / 间隔未到 / API 失败（静默不打扰）
    返回 thread 以便调用方延寿引用避免 GC，None = 没启动检查。
    """
    if not should_check_in_background(settings):
        return None

    cfg = load_release_config()
    checker = ReleaseChecker(
        owner=cfg["github_owner"],
        repo=cfg["github_repo"],
        api_base=cfg["github_api"],
        timeout=8.0,
    )
    thread = QThread(parent)
    worker = _CheckWorker(checker)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    def _on_done(latest: LatestRelease) -> None:
        settings.set(
            "update.last_check_iso",
            datetime.now(timezone.utc).isoformat(),
        )
        settings.save()
        skip = settings.get("update.skip_version", "")
        if skip and skip == latest.tag_name:
            log.info("Version %s was skipped by the user", skip)
            return
        if compare_versions(CURRENT_VERSION, latest.tag_name) >= 0:
            log.info("The installed version is current")
            return
        if on_new_version is not None:
            on_new_version(latest)

    def _on_failed(error: UpdaterError) -> None:
        log.info("Silent background update check failed: %s", error)

    worker.finished.connect(_on_done)
    worker.failed.connect(_on_failed)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.start()
    return thread
