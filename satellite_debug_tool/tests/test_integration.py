import pytest
import tempfile
import os
import numpy as np
from pathlib import Path
from satellite_debug_tool.core.protocol import FrameReceiver, DataFrame, ChannelData
from satellite_debug_tool.core.data import DataStore
from satellite_debug_tool.io.data_recorder import DataRecorder
from satellite_debug_tool.io.data_importer import DataImporter
from satellite_debug_tool.ui.chart_widget import ChartWidget


class TestDataFlow:
    """测试完整数据流"""

    def test_frame_to_datastore(self):
        """测试：FrameReceiver -> DataStore"""
        receiver = FrameReceiver()
        store = DataStore()

        # 构造测试帧
        frame = DataFrame(
            cmd_type=1,
            timestamp=1000,
            channels=[ChannelData(name="TEST_CH", value=123.45)],
        )
        store.update(frame)

        assert "TEST_CH" in store.get_all_channels()
        ch = store.get_channel("TEST_CH")
        latest = ch.get_latest()
        assert latest[0] == 1000.0
        assert np.isclose(latest[1], 123.45)


class TestRecorderImporter:
    """测试录制和导入循环"""

    def test_record_import_roundtrip(self):
        """测试：录制 -> 导入 数据一致性"""
        with tempfile.NamedTemporaryFile(suffix=".sdb", delete=False) as f:
            filepath = f.name

        try:
            recorder = DataRecorder(filepath)
            recorder.start()

            # 写入模拟帧数据 (46 bytes total)
            # Header: AA 55 [reserved=0] [cmd_type=1] [data_len=41]
            # Data: timestamp(4) + channel_count(1) + channel_name(32) + value(4)
            frame_data = bytes(
                [
                    0xAA,
                    0x55,
                    0x00,
                    0x01,
                    0x29,
                    0x00,
                    0xE8,
                    0x03,
                    0x00,
                    0x00,
                    0x01,
                    0x54,
                    0x45,
                    0x53,
                    0x54,
                    0x5F,
                    0x43,
                    0x48,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x66,
                    0xE6,
                    0xF6,
                    0x42,
                ]
            )
            recorder.write_frame(frame_data)
            recorder.stop()

            # 导入并验证
            frames = list(DataImporter.read_sdb(filepath))
            assert len(frames) > 0
        finally:
            os.unlink(filepath)


class TestChartWidget:
    """测试 ChartWidget"""

    def test_chart_set_channels(self):
        """测试通道设置"""
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            app = QApplication([])
        widget = ChartWidget()
        widget.set_channels(["CH1", "CH2"])
        widget.set_channels(["CH1", "CH2", "CH3"])
        widget.set_channels(["CH1"])
        widget.clear()
        widget.close()


class TestConfig:
    """测试配置管理"""

    def test_config_load_save(self):
        """测试配置加载和保存"""
        from satellite_debug_tool.core.config import Settings

        settings = Settings()
        original_theme = settings.get("ui.theme", "Dark")

        # 修改并保存
        settings.set("ui.theme", "Light")
        settings.save()

        # 新建实例验证
        settings2 = Settings()
        assert settings2.get("ui.theme") == "Light"

        # 恢复
        settings2.set("ui.theme", original_theme)
        settings2.save()
