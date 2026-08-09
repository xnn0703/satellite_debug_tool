# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Satellite Debug Tool — PySide6 desktop app for debugging phased-array satellite communication equipment. Real-time data curves (PyQtGraph), serial/UDP transport, 3D attitude display (OpenGL), `.sdb` v2 record/replay with embedded profile, WindTerm log parser, **offline GPS map (Leaflet + 离线 tile)**.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip3 install -r satellite_debug_tool/requirements.txt

# editable install（推荐，便于 IDE 跳转和 import 解析）
pip3 install -e satellite_debug_tool
```

## Running the Application

```bash
# 必须用 -m 方式启动（因为包内用绝对 import）
python3 -m satellite_debug_tool.main
```

## Running Tests

```bash
PYTHONPATH=. pytest satellite_debug_tool/tests              # 全套（无 editable install 时需要 PYTHONPATH）
pytest satellite_debug_tool/tests/test_frame_v2.py -v       # 单文件
pytest satellite_debug_tool/tests -k "crc"                  # 按 pattern
```

> 当前没有固定允许失败的 pytest baseline；全量测试失败时按真实回归调查。

## Architecture (Current M1–M12)

### Package Structure

```
satellite_debug_tool/
├── core/
│   ├── protocol/       # v2 协议：帧编解码、状态机解析、握手
│   ├── comm/           # 通信 worker（Serial/UDP，QThread）
│   ├── data/           # 数据存储：ChannelBuffer / DataStore / StateStore / EventLog
│   ├── profile/        # ProfileStore + ProfileCache（设备 profile 驱动 UI）
│   ├── log_parser/     # WindTerm log 解析
│   └── config.py       # 用户配置 (~/.satellite_debug_tool/settings.json)
├── ui/                 # PySide6 界面（四 Tab：Live / Playback / Log / Device）
│   ├── assets/         # Leaflet 离线地图 bundle
│   └── ...             # 各 widget（chart / dashboard / state / event / attitude / map）
├── io/                 # SDB v2 录制（DataRecorder）/ 导入（DataImporter）
└── tests/              # pytest；UI 测试用 qapp fixture

tools/
└── tile_downloader.py  # OSM tile 离线下载 CLI（python -m tools.tile_downloader）
```

### 四 Tab 数据流

```
Live Tab
  Device → SerialWorker/UdpWorker → FrameReceiverV2 → records
    ↓
  Handshake 消费 META/DEFINE/HEARTBEAT → ProfileStore (Live 独立)
  DataReport → DataStore (Live, ring 30000) → GroupedChart.refresh @5Hz
  StateReport/EventReport → StateStore/EventLog → 各 widget

Playback Tab
  .sdb v2 文件 → DataImporter.open_sdb → SdbFile.iter_records
    ↓ (profile dict 内嵌)
  ProfileStore (Playback 独立, cache=None) + DataStore (无界)
  TimeRangeControl → chart.set_x_range_sec

Log Tab
  .log 文件 → WindTermLogParser → (列名, 数据矩阵, 占位时间戳)
    ↓
  虚拟 ProfileStore (hw_type="windterm_log") + DataStore (无界, max_channels=128)
  每行 → DataReport(行号*100ms, samples) → DataStore.update

Device Tab
  共享 Live worker + frame_received
    ↓
  META_INFO / PARA_TABLE_REPORT / COMMAND_RESPONSE
    ↓
  设备信息 / 参数表读写 / OTA BEGIN-DATA-END-ABORT / 等待重启上线
```

### 关键类

- **FrameReceiverV2** ([core/protocol/frame_receiver_v2.py](satellite_debug_tool/core/protocol/frame_receiver_v2.py)): 状态机解析 v2 协议；`feed(bytes)` 返回 record 列表（DataReport / StateReport / EventReport / MetaInfo / DEFINE / Heartbeat 等）
- **Handshake** ([core/protocol/handshake.py](satellite_debug_tool/core/protocol/handshake.py)): 连接后发 4 条 REQUEST 拉 META + 三张 DEFINE，定时 tick 心跳检查
- **ProfileStore** ([core/profile/profile_store.py](satellite_debug_tool/core/profile/profile_store.py)): 按 hw_type 聚合 profile，幂等 apply_meta；M7 每个 Tab 一份独立实例
- **DataStore** ([core/data/data_store.py](satellite_debug_tool/core/data/data_store.py)): `buffer_capacity: int | None`，`None` = 无界（list 累加，Playback / Log 用）
- **ChannelBuffer** ([core/data/channel_buffer.py](satellite_debug_tool/core/data/channel_buffer.py)): 环形 ndarray 或无界 list 两种模式；默认 30000 ≈ 5min @ 100Hz
- **GroupedChartWidget** ([ui/grouped_chart_widget.py](satellite_debug_tool/ui/grouped_chart_widget.py)): profile 驱动；`set_mode("combined" / "stacked")` 切换单图/分组；`refresh(data_store)` 整批 setData
- **DataRecorder** ([io/data_recorder.py](satellite_debug_tool/io/data_recorder.py)): 异步（threading.Thread + queue）；SDB v2 文件头内嵌 profile JSON
- **WindTermLogParser** ([core/log_parser/windterm_log.py](satellite_debug_tool/core/log_parser/windterm_log.py)): 正则识别 `track_debug_print_table_header:` / `track_table_row_bynav:`，非数字列整列剔除，行号 × 100ms 占位时间戳
- **MapWidget** ([ui/map_widget.py](satellite_debug_tool/ui/map_widget.py)): Leaflet + QWebEngine 离线地图。自动检测 `~/.satellite_debug_tool/tiles/<region>/` 目录，单向 `runJavaScript` 调 JS API（setTrack / setTrackHighlight / addEvent / clear），HTML 异步加载期间 JS 调用进缓冲队列
- **DeviceView** ([ui/device_view.py](satellite_debug_tool/ui/device_view.py)): 共享 Live 连接；设备信息、参数表、OTA 状态机
- **tile_downloader** ([tools/tile_downloader.py](tools/tile_downloader.py)): OSM 离线 tile 下载 CLI；WGS84 → Web Mercator tile 坐标 + 限速 + 断点续传

### 协议 v2 摘要

帧格式：`AA 55 0D` + cmd_type(1B) + len(2B LE) + data + CRC16-CCITT(2B LE) + `EE`

- 完整规范：[doc/DEBUG设备协议接口规范_v2.md](doc/DEBUG设备协议接口规范_v2.md)
- 11 个 cmd_type（DATA_REPORT 0x01 / STATE_REPORT 0x08 / EVENT_REPORT 0x09 / META 0x04 / DEFINE 0x05~0x07 / CONTROL 0x03 / HEARTBEAT 0x0A 等）
- 三张 DEFINE 表（CHANNEL/STATE/EVENT）按需 ID 标识；上位机 UI 完全由 profile 驱动，afd01/ufd45/esa01 共用同一套渲染逻辑

### 性能优化（M6/M7）

- **双定时器解耦**：100ms 轻量更新（FPS、姿态、通道值）+ 200ms 重绘（GroupedChart / Dashboard）
- **GroupedChart 防闪烁**：固定 Y 范围 + X 滚屏阈值 1s（不每帧 setXRange）+ pyqtgraph setDownsampling("peak", auto=True) + setClipToView
- **profile 幂等**：META 5s 周期广播但只在 hw_type / 版本变化时 emit profile_changed
- **录制异步**：write_frame 非阻塞 queue.put_nowait，后台线程刷盘
- **归一化性能优化（M12）**：legend 文本节流、O(1) label 缓存、per-plot 归一化、数据曲线关闭抗锯齿

## 重要约定

- UI 在 `ui/`；业务在 `core/`；持久化在 `io/`。所有 import 用 `satellite_debug_tool.` 前缀。
- 通信 worker 继承 `BaseWorker`（QThread）emit Qt 信号；**严禁** worker 线程直接动 widget
- **每个 Tab 独立 DataStore / ProfileStore**（M7），切 Tab 不会污染数据
- 字号已固化 `small`（12px 基准）；styles.py 的 `FONT_SCALES` API 保留只为兼容 `test_styles.py`，UI 不再暴露
- 配置：`~/.satellite_debug_tool/settings.json`；profile 缓存：`~/.satellite_debug_tool/profiles/{hw_type}.json`；M8 地图 tile：`~/.satellite_debug_tool/tiles/{region}/{z}/{x}/{y}.png`
- **离线地图**：约定 GPS channel 名 `gps_lat` / `gps_lon`（可选 `gps_alt`），Playback / Log 检测到自动启用"地图"按钮，浮窗显示轨迹 + 起点(绿)/终点(红) + 所有事件 marker
- 测试名/注释多为中文；UI 测试用 `qapp` fixture 复用 QApplication 实例
- **Git commit 格式**：`type(scope): 中文描述`，type 用英文（feat/fix/perf/refactor/test/docs/chore）

## 关键文档

- [doc/optimization_plan.md](doc/optimization_plan.md) — M1–M6 整体优化计划（v1.2）
- [doc/upper_pc_function_definition_vnext.md](doc/upper_pc_function_definition_vnext.md) — 当前上位机功能定义与后续路线
- [doc/M7_plan.md](doc/M7_plan.md) — M7 Tab 化 + 字号固化 + log 解析
- [doc/M7_acceptance.md](doc/M7_acceptance.md) — M7 验收锚点 + 自评
- [doc/M7_dev_log.md](doc/M7_dev_log.md) — M7 实施日志
- [doc/M8_plan.md](doc/M8_plan.md) — M8 离线地图（GPS 轨迹 + 事件）
- [doc/M8_acceptance.md](doc/M8_acceptance.md) — M8 验收锚点 + 自评
- [doc/M8_dev_log.md](doc/M8_dev_log.md) — M8 实施日志
- [doc/M10_plan.md](doc/M10_plan.md) / [doc/M11_plan.md](doc/M11_plan.md) / [doc/M12_plan.md](doc/M12_plan.md) — 后续局部优化计划
- [doc/DEBUG设备协议接口规范_v2.md](doc/DEBUG设备协议接口规范_v2.md) — 协议权威规范
- [doc/development_log.md](doc/development_log.md) — M1–M6 实施日志
- [doc/acceptance_log.md](doc/acceptance_log.md) — F-/A- 系列验收跟踪
