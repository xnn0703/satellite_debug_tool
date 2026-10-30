"""用户产物的共享呈现模块合同；其余工作区只随运维产物发行。"""

from pathlib import Path


USER_UI_MODULES = frozenset("""
application_icon application_window_actions attitude_widget beam_polar_widget
chart_group_dialog chart_group_titles chart_widget component_temperature_window
customer_device_dialog customer_device_list customer_endpoint_pages customer_maintenance_view
customer_ota_adapter customer_overview_view customer_playback_view customer_rf_control_view
customer_session_bundle customer_tcp_shell_dialog customer_workspace tcp_shell_workspace device_model_resources flow_layout gnss_widget
grouped_chart_widget icons lazy_view_host live_view orbit_widget ota_status_text qss
semantic_style settings_dialog stl_loader styles time_range_control update_dialog user_window
view_lifecycle
""".split())


USER_SHELL_MODULES = frozenset({
    "satellite_debug_tool.ui.customer_tcp_shell_dialog",
    "satellite_debug_tool.ui.tcp_shell_workspace",
    "satellite_debug_tool.core.tcp_shell.controller",
    "satellite_debug_tool.core.tcp_shell.worker",
    "satellite_debug_tool.core.tcp_shell.transport",
    "satellite_debug_tool.core.tcp_shell.ymodem_sender",
    "satellite_debug_tool.core.tcp_shell.recorder",
    "satellite_debug_tool.core.tcp_shell.rx_bridge",
    "satellite_debug_tool.core.tcp_shell.terminal",
})


def operations_modules(root: Path) -> tuple[str, ...]:
    names = ["satellite_debug_tool.ui." + path.stem
             for path in (root / "satellite_debug_tool/ui").glob("*.py")
             if path.stem != "__init__" and path.stem not in USER_UI_MODULES]
    names.extend("satellite_debug_tool.core.production." + path.stem
                 for path in (root / "satellite_debug_tool/core/production").glob("*.py")
                 if path.stem not in ("__init__", "fixture_telemetry"))
    return tuple(sorted(names))
