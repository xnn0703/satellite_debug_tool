import sys
from PySide6.QtWidgets import QApplication
from satellite_debug_tool.core.config import Settings
from satellite_debug_tool.i18n import initialize_translation_manager
from satellite_debug_tool.ui import styles as S
from satellite_debug_tool.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    settings = Settings()
    initialize_translation_manager(app, settings)
    # Mission Console：先加载打包字体（IBM Plex 若存在），再设全局界面字体
    S.load_bundled_fonts()
    S.apply_global_font(app, scale="small", base_px=13)
    window = MainWindow(settings=settings)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
