"""UI exports are loaded only by the selected application composition."""
from importlib import import_module
_EXPORTS = {'ConnectionDialog': ('satellite_debug_tool.ui.connection_dialog', 'ConnectionDialog'),
 'GroupedChartWidget': ('satellite_debug_tool.ui.grouped_chart_widget', 'GroupedChartWidget'),
 'LiveView': ('satellite_debug_tool.ui.live_view', 'LiveView'),
 'LogView': ('satellite_debug_tool.ui.log_view', 'LogView'),
 'MainWindow': ('satellite_debug_tool.ui.main_window', 'MainWindow'),
 'MapWidget': ('satellite_debug_tool.ui.map_widget', 'MapWidget'),
 'PlaybackView': ('satellite_debug_tool.ui.playback_view', 'PlaybackView'),
 'ProductionWorkspace': ('satellite_debug_tool.ui.production_workspace', 'ProductionWorkspace'),
 'TimeRangeControl': ('satellite_debug_tool.ui.time_range_control', 'TimeRangeControl')}
__all__ = list(_EXPORTS)

def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, symbol = _EXPORTS[name]
    value = getattr(import_module(module), symbol)
    globals()[name] = value
    return value
