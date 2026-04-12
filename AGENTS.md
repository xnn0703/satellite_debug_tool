# AGENTS.md

## Project Overview
PySide6-based satellite communication debug tool. Single-package Python project under `satellite_debug_tool/`.

## Commands
```bash
# Run GUI (from repo root)
python satellite_debug_tool/main.py

# Run tests
pytest satellite_debug_tool/tests

# Install dependencies
pip install -r satellite_debug_tool/requirements.txt

# Editable install (for IDE imports)
pip install -e satellite_debug_tool
```

## Package Structure
- `satellite_debug_tool/core` — protocol parsing (`core/protocol/`), comm workers (`core/comm/`), data store
- `satellite_debug_tool/io` — `DataRecorder` (`.sdb` format), `DataImporter`
- `satellite_debug_tool/ui` — PySide6 widgets: `MainWindow`, `ChartWidget`, `ConnectionDialog`

## Key Patterns
- All imports use `satellite_debug_tool.` prefix (e.g., `from satellite_debug_tool.core.protocol import FrameReceiver`)
- Protocol: frame parsing in `core/protocol/` (`frame_receiver.py`, `data_frame.py`, `crc16.py`)
- Config stored at `~/.satellite_debug_tool/settings.json` (created on first run)
- UI uses PySide6 (not PyQt5)

## Testing
- Tests in `satellite_debug_tool/tests/`
- `test_integration.py` includes full data flow tests (FrameReceiver → DataStore → DataRecorder → DataImporter roundtrip)
- `.sdb` format used for recorded data files

## Documentation
- `doc/开发文档.md` — general dev docs
- `doc/DEBUG设备协议接口规范.md` — device protocol spec
