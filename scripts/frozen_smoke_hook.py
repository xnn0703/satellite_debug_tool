"""在冻结入口导入前建立烟测异常证据，避免 windowed 异常对话框阻塞 CI。"""

from satellite_debug_tool.smoke_diagnostics import install

install()
