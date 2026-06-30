# 工程卫生收口开发记录

## 2026-06-30

### 计划

按 `engineering_hygiene_plan.md` 做一次小范围工程卫生收口，优先保证安装元数据、协议文档、仿真交付记录与当前源码/测试一致。

### 实施记录

- `satellite_debug_tool/pyproject.toml` 改为动态读取 `satellite_debug_tool.__version__`。
- 显式声明 `satellite_debug_tool.*` 包，替代旧的 `core*` / `ui*` / `io*` 顶层包发现规则。
- 将 `PyOpenGL`、`py7zr`、`psutil` 同步进项目依赖，覆盖 3D 与 updater 运行时。
- 为 `satellite_debug_tool.ui` 声明地图 HTML、Leaflet、字体等静态资源。
- `doc/DEBUG设备协议接口规范_v2.md` 同步当前 `MAX_DATA_LENGTH=1024` 与 1024 字节长帧缓冲说明。
- `doc/simulation_delivery.md` 同步当前全量测试复核结果为 `488 passed`。

### 验证记录

- 临时 venv 验证：
  - `python3 -m venv /tmp/sdt-pkg-test-*`
  - `python -m pip install -e satellite_debug_tool --no-deps`
  - 从 `/tmp` 导入 `satellite_debug_tool` 成功。
  - `importlib.metadata.version("satellite_debug_tool") == "1.0.0"`。
  - `MAX_DATA_LENGTH == 1024`，`DATA_REPORT_MAX_CHANNELS == 32`。
- 全量测试：
  - `PYTHONPATH=. pytest satellite_debug_tool/tests`
  - 结果：`488 passed, 4 warnings in 19.32s`。
  - warning 为 Python 3.13 下 `datetime.utcnow()` 弃用提示，与本次改动无关。
- 工作区保护：
  - 未编辑既有脏文件 `.claude/settings.local.json` 与 `AGENTS.md`。
