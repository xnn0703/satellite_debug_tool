"""Operations edition entry point."""
import multiprocessing
import time
_ENTRY_STARTED = time.perf_counter()
from satellite_debug_tool.bootstrap import run_application
from satellite_debug_tool.edition import Edition
from satellite_debug_tool.ui.main_window import MainWindow


def main():
    multiprocessing.freeze_support()
    run_application(Edition.OPS, MainWindow, started=_ENTRY_STARTED)


if __name__ == "__main__":
    main()
