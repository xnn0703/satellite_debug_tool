# AGENTS.md

## 运行命令

```bash
# 启动 GUI（必须用 -m，包内绝对 import）
python3 -m satellite_debug_tool.main

# 测试（无 editable install 时需要 PYTHONPATH）
PYTHONPATH=. pytest satellite_debug_tool/tests
pytest satellite_debug_tool/tests/test_frame_v2.py -v      # 单文件
pytest satellite_debug_tool/tests -k "crc"                 # 按 pattern

# 依赖安装
pip3 install -r satellite_debug_tool/requirements.txt
pip3 install -e satellite_debug_tool                       # editable（IDE 跳转需要）
```

没有 linter / typecheck / formatter 配置。唯一验证手段是 pytest。

## 已知测试基线

当前没有固定允许失败的 pytest baseline。若全量测试失败，优先按真实回归调查，不要把失败视作预期。

## 架构

PySide6 桌面应用，调试相控阵卫星通信设备。四 Tab 设计（Live / Playback / Log / Device），Live/Playback/Log 各自独立 DataStore + ProfileStore，切 Tab 不污染数据；Device 共享 Live 的连接和帧广播。

### 数据流

```
Live:    Device → SerialWorker/UdpWorker → FrameReceiverV2 → DataStore (ring 30000) → GroupedChart @5Hz
Playback: .sdb v2 → DataImporter → DataStore (无界) + 内嵌 profile
Log:     .log → WindTermLogParser → 虚拟 ProfileStore (hw_type="windterm_log") + DataStore (无界, max=128)
Device:  共享 Live worker/frame stream → 参数表 / COMMAND_RESPONSE / OTA 状态机
```

### 包结构

- `core/protocol/` — v2 协议帧解析状态机 (`FrameReceiverV2`)、握手 (`Handshake`)、CRC16
- `core/comm/` — QThread worker（`SerialWorker` / `UdpWorker`，继承 `BaseWorker`）
- `core/data/` — `ChannelBuffer`（环形 ndarray 或无界 list）、`DataStore`、`StateStore`、`EventLog`
- `core/profile/` — `ProfileStore` + `ProfileCache`，设备 profile 驱动 UI
- `core/log_parser/` — WindTerm 日志解析
- `core/config.py` — `Settings` 类，JSON 存 `~/.satellite_debug_tool/settings.json`
- `ui/` — PySide6 组件，`MainWindow` 为主入口
- `io/` — `DataRecorder`（异步，SDB v2 内嵌 profile JSON）、`DataImporter`
- `tools/` — `tile_downloader.py`（OSM 离线 tile CLI）
- `updater/` — 自动升级模块（py7zr 解压 7z 分卷）

### 关键约定

- 所有 import 用 `satellite_debug_tool.` 前缀（绝对导入）
- UI 在 `ui/`，业务在 `core/`，持久化在 `io/`
- worker 线程**严禁**直接操作 widget，必须通过 Qt 信号
- 协议帧格式：`AA 55 0D` + cmd_type(1B) + len(2B LE) + data + CRC16-CCITT(2B LE) + `EE`
- 测试在 `satellite_debug_tool/tests/`，名称和注释多为中文；UI 测试用 `qapp` fixture（`conftest.py` 设置 `QT_QPA_PLATFORM=offscreen` + `SATELLITE_NO_UPDATE_CHECK=1`）
- 字号已固化 `small`（12px 基准），`styles.py` 的 `FONT_SCALES` API 仅保留兼容 `test_styles.py`
- 离线地图约定 GPS channel 名 `gps_lat` / `gps_lon`（可选 `gps_alt`）

## 构建与发版

- macOS 本地打包：`./scripts/build_macos.sh`（自动 venv + 装依赖 + ditto 出 zip）
- Windows CI 打包：`.github/workflows/build.yml`（PyInstaller，7z 分卷上传 Gitee + GH Release）
- mac 不走 CI（PyInstaller .app symlink + 7z 兼容性问题）
- 版本号在 `satellite_debug_tool/__init__.py.__version__`，发版打 `v*` tag 触发 CI
- `release.config.json` 配置 Gitee/GitHub 仓库地址，updater 用 `release_config.py` 读取

## 关键文档

- `doc/DEBUG设备协议接口规范_v2.md` — 协议权威规范
- `doc/upper_pc_function_definition_vnext.md` — 当前上位机功能定义与后续路线
- `doc/development_log.md` — M1–M6 实施日志
- `doc/M7_dev_log.md` ~ `doc/M12_plan.md` — 各里程碑日志/计划
- `doc/RELEASING.md` — 发版流程
- `BUILDING.md` — 打包可执行文件完整指南
