"""用户版固定入口。"""
import time
_ENTRY_STARTED = time.perf_counter()
from satellite_debug_tool.bootstrap import run_application
from satellite_debug_tool.edition import Edition
from satellite_debug_tool.ui.user_window import UserWindow


def main():
    run_application(Edition.USER, UserWindow, started=_ENTRY_STARTED)


if __name__ == "__main__":
    main()
