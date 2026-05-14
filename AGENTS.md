# AGENTS.md

## 项目概述
PySide6 桌面应用程序，用于调试相控阵卫星通信设备。实时曲线显示（PyQtGraph）、串口/UDP 通信、3D 姿态显示（Roll/Pitch/Yaw）、数据录制回放（`.sdb`/`.csv`）、深色/浅色主题切换。

## 运行命令
```bash
# 从仓库根目录运行 GUI
python satellite_debug_tool/main.py

# 或用 -m 模块方式
python -m satellite_debug_tool.main

# 运行测试
pytest satellite_debug_tool/tests

# 运行单个测试文件
pytest satellite_debug_tool/tests/test_frame_receiver.py -v

# 按模式匹配运行测试
pytest satellite_debug_tool/tests/ -k "crc"

# 安装依赖
pip install -r satellite_debug_tool/requirements.txt

# 可编辑安装（IDE 导入需要）
pip install -e satellite_debug_tool
```

## 包结构
- `satellite_debug_tool/core` — 协议解析（`core/protocol/`）、通信工作线程（`core/comm/`）、数据存储
- `satellite_debug_tool/core/protocol/` — 帧解析：`frame_receiver.py`、`frame_v2.py`、`codec_v2.py`、`crc16.py`
- `satellite_debug_tool/core/comm/` — QThread 工作线程：`serial_worker.py`、`udp_worker.py`、`base_worker.py`
- `satellite_debug_tool/core/data/` — `ChannelBuffer`（环形缓冲区）、`DataStore`、`EventLog`、`StateStore`
- `satellite_debug_tool/core/profile/` — `ProfileStore`，配置缓存
- `satellite_debug_tool/io` — `DataRecorder`（`.sdb` 二进制格式）、`DataImporter`
- `satellite_debug_tool/ui` — PySide6 组件：`MainWindow`、`ChartWidget`、`GroupedChartWidget`、`AttitudeWidget`（3D OpenGL）、`ConnectionDialog`

## 架构

### 数据流
```
设备 → SerialWorker/UdpWorker → FrameReceiver（状态机）→ DataFrame
                                                         ↓
                                               ChannelBuffer（环形缓冲区，2000 采样点）
                                                         ↓
                                               DataStore（管理器）
                                                         ↓
                                               ChartWidget（PyQtGraph）
```

### 关键类
- **FrameReceiver**：二进制协议状态机（0xAA 0x55 帧头，CRC16-CCITT 校验）
- **BaseWorker**：QThread 工作线程，信号：`connected`、`disconnected`、`error`、`data_received`
- **ChannelBuffer**：每通道环形缓冲区，支持 `append()` 和 `get_latest()`
- **DataStore**：管理所有通道，`update(frame)`、`get_channel(name)`、`get_all_channels()`
- **AttitudeWidget**：3D OpenGL 飞机模型，显示 Roll/Pitch/Yaw
- **Settings**：JSON 配置，存储在 `~/.satellite_debug_tool/settings.json`

## 关键约定
- 所有导入使用 `satellite_debug_tool.` 前缀（如 `from satellite_debug_tool.core.protocol import FrameReceiver`）
- UI 逻辑放在 `ui/`，业务逻辑在 `core/`，持久化在 `io/`
- 通信工作线程继承 `BaseWorker`（QThread），通过 Qt 信号实现线程安全 UI 更新
- 测试在 `satellite_debug_tool/tests/`，很多测试名称和注释是中文的
- 使用 PySide6（非 PyQt5）
- 配置存储在 `~/.satellite_debug_tool/settings.json`（首次运行自动创建）

## 协议格式
二进制帧：`AA 55`（帧头）→ device_type → cmd_type → len（小端）→ data → crc16 → `EE`（帧尾）

## 测试
- `test_integration.py` — 完整数据流测试（FrameReceiver → DataStore → DataRecorder → DataImporter 往返）
- 录制数据格式：`.sdb`（二进制）

## 文档
- `doc/开发文档.md` — 开发文档
- `doc/DEBUG设备协议接口规范.md` — 设备协议接口规范

## CI/CD
- GitHub Actions：`.github/workflows/build.yml` — 用 PyInstaller 构建 macOS/Windows 安装包
- 构建脚本：`scripts/build_macos.sh`、`scripts/build_windows.bat`
