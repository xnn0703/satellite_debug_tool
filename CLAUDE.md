# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Satellite Debug Tool — a PySide6 desktop application for debugging phased-array satellite communication equipment. Displays real-time data curves, supports serial/UDP communication, and can record/playback data in `.sdb` (binary) and `.csv` formats.

## Running the Application

```bash
# From repository root
python satellite_debug_tool/main.py

# Or with editable install
pip install -e satellite_debug_tool
python -m satellite_debug_tool.main
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
│   └── data/       # ChannelBuffer (ring buffer), DataStore
├── ui/             # PySide6 widgets — MainWindow, ChartWidget, ConnectionDialog
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
- **BaseWorker**: QThread with signals — `signal_connected`, `signal_disconnected`, `signal_data(bytes)`
- **ChannelBuffer**: Ring buffer per channel, capacity 2000 samples
- **DataStore**: Manages all channels, provides `update(frame)`, `get_channel(name)`

### Protocol

Binary frame format: `AA 55` (header) → device_type → cmd_type → len (little-endian) → data → crc16 → `EE` (footer)

## Important Conventions

- UI logic stays in `satellite_debug_tool/ui`; business logic in `core`, persistence in `io`
- Communication workers inherit `BaseWorker` (QThread) and emit Qt signals for thread-safe UI updates
- Tests are in `satellite_debug_tool/tests`; many test names and comments are in Chinese
- Configuration is stored in `~/.satellite_debug_tool/settings.json`
