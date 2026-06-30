# 工程卫生收口计划

## 背景

本次探索发现当前源码和测试是健康的，但有三处会影响后续开发/发版判断：

- editable install 的元数据仍按旧布局发布 `core` / `ui` / `io` 顶层包，仓库外无法 `import satellite_debug_tool`。
- `pyproject.toml` 版本为 `0.1.0`，与 `satellite_debug_tool.__version__ = "1.0.0"` 不一致。
- 协议与仿真交付文档中存在已由当前代码/测试证实的漂移。

## 范围

1. 修正 `satellite_debug_tool/pyproject.toml`：
   - 以 `satellite_debug_tool.__version__` 作为动态版本源。
   - 显式发布 `satellite_debug_tool.*` 包。
   - 同步运行时依赖到当前 `requirements.txt` 覆盖范围。
   - 包含地图 HTML/Leaflet 静态资源。
2. 同步协议文档中的 `MAX_DATA_LENGTH=1024` 与帧缓冲说明。
3. 同步仿真交付文档中的当前全量测试数量。

## 不在范围

- 不重构运行时代码。
- 不处理 OTA 设计风险。
- 不清理 `.claude/`、`.mimocode/`、handoff 截图等既有工作区脏文件。
- 不主动提交。

## 验证

- 在临时 venv 中 `pip install -e satellite_debug_tool --no-deps` 后，从仓库外导入 `satellite_debug_tool`。
- 运行 `PYTHONPATH=. pytest satellite_debug_tool/tests`。
