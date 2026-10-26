"""两种产物共用启动流程，窗口工厂由固定入口提供。"""

import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from satellite_debug_tool.core.application_logging import configure_application_logging
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.edition import Edition, build_edition
from satellite_debug_tool.smoke_diagnostics import stage
from satellite_debug_tool.i18n import initialize_translation_manager
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.application_icon import application_icon


def run_application(edition, window_factory, *, started=None):
    edition = Edition(edition)
    if getattr(sys, "frozen", False) and build_edition() is not edition:
        raise RuntimeError("entry point differs from the packaged edition")
    production = "--production" in sys.argv[1:]
    if edition is Edition.USER and any(arg.startswith(("--production", "--engineering", "--edition")) for arg in sys.argv[1:]):
        raise SystemExit("User edition supports customer features only")
    import os
    import tempfile
    import time
    smoke = "--smoke-test" in sys.argv[1:]
    started = time.perf_counter() if started is None else started
    temporary = tempfile.TemporaryDirectory(prefix="sdt-frozen-smoke-") if smoke else None
    if smoke:
        os.environ["SATELLITE_UPDATE_CHECK"] = "0"
    stage("create-application")
    app = QApplication([arg for arg in sys.argv if arg not in ("--production", "--smoke-test") and not arg.startswith("--smoke-output=")])
    app.setWindowIcon(application_icon())
    settings = Settings(edition=edition, **({"config_directory": temporary.name} if smoke else {}))
    if smoke:
        settings.set("customer.devices", [{"ip": "127.0.0.1", "port": 49999}])
        settings.set("customer.active_endpoint", {"ip": "127.0.0.1", "port": 49999})
    configure_application_logging(log_path=Path(settings.config_directory) / "logs" / "application.log")
    initialize_translation_manager(app, settings)
    S.load_bundled_fonts()
    S.apply_global_font(app, scale="small", base_px=13)
    stage("create-window")
    window = window_factory(settings=settings)
    if production:
        window.unlock_production_for_session()
    stage("show-window")
    window.show()
    if smoke:
        import json
        import psutil
        from satellite_debug_tool import __version__
        app.processEvents()
        initial = {"first_window_seconds": round(time.perf_counter() - started, 3),
                   "initial_rss_bytes": psutil.Process().memory_info().rss,
                   "initial_thread_count": psutil.Process().num_threads()}
        stage("overview-panels")
        overview = window._customer.session_page("overview", ensure=True)
        if overview is None:
            raise RuntimeError("overview page is required")
        for panel in (overview._signal_panel, overview._component_panel):
            if overview._root_layout.indexOf(panel) < 0 or not panel.isVisibleTo(window):
                raise RuntimeError("overview requires visible SNR and component panels")
            if not overview.rect().contains(panel.geometry()):
                raise RuntimeError("overview panel exceeds the page bounds")
        if getattr(overview, "_attitude", None) is not None:
            if not overview._attitude.try_load_device_model("afd01"):
                raise RuntimeError("bundled model could not be loaded")
            app.processEvents()
        stage("customer-pages")
        for page in window._customer._page_ids:
            window._customer.set_page(page)
            app.processEvents()
        stage("shell-dialog")
        shell = window._customer.open_tcp_shell_dialog()
        app.processEvents()
        if window._customer.open_tcp_shell_dialog() is not shell or shell.workspace._controller.worker is not None:
            raise RuntimeError("shell did not retain its idle dialog")
        if not shell.close():
            raise RuntimeError("shell dialog shutdown was refused")
        app.processEvents()
        if edition is Edition.OPS:
            stage("engineering-pages")
            window.unlock_engineering_for_session()
            for index in range(window._tabs.count()):
                window._tabs.setCurrentIndex(index)
                app.processEvents()
            window._ensure_production_workspace()
        forbidden = ("ui.main_window", "ui.production_workspace", "ui.device_view", "core.motion_platform",
                     "core.external_power_monitor", "core.iperf_test", "core.production.reporting")
        loaded = [name for name in sys.modules if any(name.startswith("satellite_debug_tool." + key) for key in forbidden)]
        if edition is Edition.USER and loaded:
            raise RuntimeError("user edition loaded operations modules: " + repr(loaded))
        result = {**initial, "edition": edition.value, "version": __version__, "qpa_platform": app.platformName(), "pages": window._customer._page_ids,
                  "startup_seconds": round(time.perf_counter() - started, 3),
                  "rss_bytes": psutil.Process().memory_info().rss,
                  "thread_count": psutil.Process().num_threads(), "operations_modules": loaded, "shell_dialog": "PASS", "overview_panels": "PASS"}
        destination = next((arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--smoke-output=")), "")
        if destination:
            Path(destination).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)
        stage("shutdown")
        if not window.close():
            raise RuntimeError("frozen smoke shutdown was refused")
        app.processEvents()
        import logging
        logging.shutdown()
        temporary.cleanup()
        sys.exit(0)
    sys.exit(app.exec())
