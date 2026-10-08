import sys
from PySide6.QtWidgets import QApplication
from satellite_debug_tool.core.application_logging import configure_application_logging
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.i18n import initialize_translation_manager
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.main_window import MainWindow


def main():
    configure_application_logging()
    production_requested = "--production" in sys.argv[1:]
    qt_argv = [arg for arg in sys.argv if arg != "--production"]
    app = QApplication(qt_argv)
    settings = Settings()
    initialize_translation_manager(app, settings)
    # Mission Console：先加载打包字体（IBM Plex 若存在），再设全局界面字体
    S.load_bundled_fonts()
    S.apply_global_font(app, scale="small", base_px=13)
    window = MainWindow(settings=settings)
    if production_requested:
        window.unlock_production_for_session()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
