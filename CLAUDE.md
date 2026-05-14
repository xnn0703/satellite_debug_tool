# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Satellite Debug Tool — a PySide6 desktop application for debugging phased-array satellite communication equipment. Displays real-time data curves (PyQtGraph), supports serial/UDP communication, 3D attitude display (Roll/Pitch/Yaw via OpenGL), and can record/playback data in `.sdb` (binary) and `.csv` formats. Dark/Light theme switching supported.

## Running the Application

```bash
# 必须使用 -m 方式运行（不能用 python satellite_debug_tool/main.py）
python3 -m satellite_debug_tool.main

# 或先安装 editable 包
pip3 install -e satellite_debug_tool --user
python3 -m satellite_debug_tool.main
```

## Running Tests

```bash
pytest satellite_debug_tool/tests
pytest satellite_debug_tool/tests/test_frame_receiver.py -v   # single test file
pytest satellite_debug_tool/tests/ -k "crc"                    # run tests matching pattern
```

## Architecture

### Package Structure

```
satellite_debug_tool/
├── core/           # Business logic (protocol, comm, data)
│   ├── protocol/   # Frame parsing, CRC16校验, data structures
│   ├── comm/       # QThread-based workers for serial/UDP
│   ├── data/       # ChannelBuffer (ring buffer), DataStore
│   └── config.py   # Settings (JSON config at ~/.satellite_debug_tool/settings.json)
├── ui/             # PySide6 widgets — MainWindow, ChartWidget, AttitudeWidget
└── io/             # DataRecorder (.sdb), DataImporter (.sdb/.csv)
```

### Data Flow

```
Device → SerialWorker/UdpWorker → FrameReceiver (state machine) → DataFrame
                                                                        ↓
                                                              ChannelBuffer (ring buffer)
                                                                        ↓
                                                              DataStore (manager)
                                                                        ↓
                                                              ChartWidget (PyQtGraph)
```

### Key Classes

- **FrameReceiver**: State machine parsing binary protocol (0xAA 0x55 header, CRC16-CCITT)
- **BaseWorker**: QThread with signals — `connected`, `disconnected`, `error`, `data_received`
- **ChannelBuffer**: Ring buffer per channel, capacity 2000 samples, supports `append()` and `get_latest()`
- **DataStore**: Manages all channels, provides `update(frame)`, `get_channel(name)`, `get_all_channels()`
- **AttitudeWidget**: 3D OpenGL aircraft model (PyQtGraph) displaying Roll/Pitch/Yaw from selected channels
- **Settings**: JSON-based config persisted to `~/.satellite_debug_tool/settings.json`

### Protocol Helpers

- **build_debug_control_frame(enabled: bool)**: Build CMD_DEBUG_CONTROL frame (0x03) to enable/disable device debug output

### Protocol

Binary frame format: `AA 55` (header) → device_type → cmd_type → len (little-endian) → data → crc16 → `EE` (footer)

## Important Conventions

- UI logic stays in `satellite_debug_tool/ui`; business logic in `core`, persistence in `io`
- Communication workers inherit `BaseWorker` (QThread) and emit Qt signals for thread-safe UI updates
- Tests are in `satellite_debug_tool/tests`; many test names and comments are in Chinese
- Configuration is stored in `~/.satellite_debug_tool/settings.json`
