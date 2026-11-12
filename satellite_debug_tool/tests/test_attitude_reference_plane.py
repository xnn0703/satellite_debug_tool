"""安装参考平面回归：所有模型显示共享的水平零姿态参考面。"""

from __future__ import annotations

import pytest
from pyqtgraph.opengl.items.GLGridItem import GLGridItem

from satellite_debug_tool.ui.attitude_widget import AttitudeWidget


def test_reference_plane_is_adaptive_to_model_radius(qapplication_session) -> None:
    """参考面按当前模型包围球半径自适应：任意尺寸模型平面都在球外。"""
    import numpy as np

    widget = AttitudeWidget()
    try:
        assert isinstance(widget._grid, GLGridItem)
        assert widget._grid in widget._gl_view.items

        # 占位机体（半尺寸 0.6/1.8/0.08）包围球半径 ≈1.899。
        expected_radius = float(np.sqrt(0.6**2 + 1.8**2 + 0.08**2))
        expected_z = -(expected_radius + 0.35)
        assert widget._reference_plane_z == pytest.approx(expected_z, abs=0.01)
        size = max(7.0, round((expected_radius + 0.7) * 2.0, 1))
        assert widget._grid.size() == [size, size, 1.0]
        assert widget._grid.spacing() == [size / 14.0, size / 14.0, 1.0]

        # 姿态变化不改变参考面位置。
        widget.update_attitude(20.0, -10.0, 30.0, "roll", "pitch", "yaw")
        assert widget._reference_plane_z == pytest.approx(expected_z, abs=0.01)
        assert widget._grid.size() == [size, size, 1.0]
    finally:
        widget.close()
        widget.deleteLater()
