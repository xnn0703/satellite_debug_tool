"""WindTermLogParser 单测（M7-S6）。"""

from __future__ import annotations

import numpy as np
import pytest

from satellite_debug_tool.core.log_parser import WindTermLogParser


_HEADER_LINE = (
    "[2026-05-14 19:29:57] I(0) track_debug_print_table_header: "
    "state hcal gps yaw pitch roll snr_raw vld"
)

_ROW_LINE_1 = (
    "[2026-05-14 19:29:57] I(0) track_table_row_bynav: "
    "GLB 0 0 +357.61 +0.03 +0.46 0.00 1"
)
_ROW_LINE_2 = (
    "[2026-05-14 19:29:58] I(0) track_table_row_bynav: "
    "GLB 0 0 +357.62 +0.04 +0.46 0.01 1"
)
_ROW_LINE_BROKEN = (
    "[2026-05-14 19:29:58] I(0) track_table_row_bynav: "
    "GLB 0 +357.62 +0.04 +0.46 0.01 1"   # 少了一列
)


class TestHeader:
    def test_no_header_returns_empty(self):
        result = WindTermLogParser.parse_text(_ROW_LINE_1)
        assert result.columns == []
        assert result.data.shape == (0, 0)
        # 行被识别为数据行但没表头 → 算 skipped
        assert result.total_data_rows == 1
        assert result.skipped_rows == 1

    def test_header_only(self):
        result = WindTermLogParser.parse_text(_HEADER_LINE)
        assert result.columns == []
        assert result.total_data_rows == 0
        assert result.skipped_rows == 0

    def test_multiple_headers_use_latest(self):
        """如果有多次表头，应该用最近一次。"""
        # 第一次表头 3 列，第二次表头 8 列；数据行 8 列
        text = "\n".join([
            "[t] I(0) track_debug_print_table_header: a b c",
            _HEADER_LINE,   # 8 列
            _ROW_LINE_1,    # 8 个值
        ])
        result = WindTermLogParser.parse_text(text)
        # 第二次表头胜出
        # 注意：state 列是 "GLB" 非数字，会被剔除 → 剩 7 列
        assert "state" not in result.columns
        assert "vld" in result.columns


class TestNonNumericColumnDropping:
    def test_drops_state_column(self):
        text = "\n".join([_HEADER_LINE, _ROW_LINE_1, _ROW_LINE_2])
        result = WindTermLogParser.parse_text(text)
        assert "state" in result.dropped_non_numeric_cols
        assert "state" not in result.columns
        # 其余 7 列保留
        assert result.columns == ["hcal", "gps", "yaw", "pitch", "roll", "snr_raw", "vld"]
        assert result.data.shape == (2, 7)
        assert result.data.dtype == np.float32

    def test_values_correct(self):
        text = "\n".join([_HEADER_LINE, _ROW_LINE_1, _ROW_LINE_2])
        result = WindTermLogParser.parse_text(text)
        # 第 2 行第 4 列 = yaw（在剔除 state 后）= +357.62
        yaw_idx = result.columns.index("yaw")
        assert result.data[1, yaw_idx] == pytest.approx(357.62, abs=1e-2)


class TestColumnMismatch:
    def test_broken_row_skipped(self):
        text = "\n".join([_HEADER_LINE, _ROW_LINE_1, _ROW_LINE_BROKEN, _ROW_LINE_2])
        result = WindTermLogParser.parse_text(text)
        assert result.skipped_rows == 1
        assert result.total_data_rows == 3
        assert result.data.shape == (2, 7)


class TestTimestamps:
    def test_placeholder_step_100ms(self):
        text = "\n".join([_HEADER_LINE] + [_ROW_LINE_1] * 5)
        result = WindTermLogParser.parse_text(text)
        assert result.timestamps_ms.shape == (5,)
        # 行号 0,1,2,3,4 → 0,100,200,300,400 ms
        np.testing.assert_array_equal(
            result.timestamps_ms,
            np.array([0.0, 100.0, 200.0, 300.0, 400.0]),
        )


class TestEmptyInput:
    def test_empty_text(self):
        result = WindTermLogParser.parse_text("")
        assert result.columns == []
        assert result.total_data_rows == 0

    def test_all_non_numeric_columns(self):
        text = "\n".join([
            "[t] I(0) track_debug_print_table_header: state ins_st stat",
            "[t] I(0) track_table_row_bynav: GLB INACTIVE ALGN",
        ])
        result = WindTermLogParser.parse_text(text)
        assert result.columns == []
        assert set(result.dropped_non_numeric_cols) == {"state", "ins_st", "stat"}


class TestRealisticSample:
    def test_user_supplied_lines(self):
        """跟用户提供的真实 log 摘录一致：65 列，state 与 ins_st/eskf/stat/base/lazy 是字符串。"""
        header = (
            "[2026-05-14 19:29:57] I(0) track_debug_print_table_header: "
            "state hcal gps yaw pitch roll ywcomp snr_raw scan_loss snr_norm "
            "ofaxis vld glb_max gyc wide_max wyc cur_opt ph az_bias el_bias "
            "nicnt ajrst lh_mean lh_peak lh_loss rmp_seen base_snr ins_st "
            "ins_pt bp_pt iyaw ipitch iroll iy_std ip_std ir_std iN_std iE_std "
            "iU_std inspvax_n eskf stat bgx bgy bgz bax bay baz acapl zar y_std "
            "p_std r_std gpos gvel gcog gcall gsz gsd acrej hreset sobs alpha "
            "gat step sig base lazy"
        )
        row = (
            "[2026-05-14 19:29:57] I(0) track_table_row_bynav: "
            "GLB 0 0 +357.61 +0.03 +0.46 +0.00 0.00 0.01 0.00 0.00 1 0.00 +0.0 "
            "0.00 +0.0 0.00 0 +0.000 +0.000 0 0 0.00 -100.00 0 0 0.00 INACTIVE "
            "0 0 +0.00 +0.00 +0.00 0.00 0.00 0.00 0.000 0.000 0.000 2420 ALGN 1 "
            "+0.2227 +0.6401 -0.1910 +0.000 +0.000 +0.000 0 2491 152.06 2.24 "
            "3.68 0 0 0 0 3129 0 0 0 0 0.400 1.000 0.300 0.30 0.00 0"
        )
        text = "\n".join([header, row, row])
        result = WindTermLogParser.parse_text(text)
        # 字符串列: state(GLB), ins_st(INACTIVE), eskf(ALGN)
        # 注意：stat 在该行是 "1"，被识别为数字保留
        for col in ("state", "ins_st", "eskf"):
            assert col in result.dropped_non_numeric_cols, (
                f"{col} should be dropped"
            )
        # 数字列应被保留
        for col in ("yaw", "pitch", "roll", "snr_raw", "snr_norm", "ins_pt"):
            assert col in result.columns, f"{col} should be kept"
        # 68 列 - 3 字符串列 (state, ins_st, eskf) = 65
        assert len(result.columns) == 65
        assert result.data.shape == (2, 65)


class TestProgressCallback:
    def test_progress_called(self):
        text = "\n".join([_HEADER_LINE] + [_ROW_LINE_1] * 100)
        captured: list[int] = []
        WindTermLogParser._parse_lines(text.splitlines(), progress_cb=captured.append, progress_every=20)
        # 5*20=100 + 1 header = 101 行；progress_every=20 触发 5 次（行 20/40/60/80/100）
        assert len(captured) == 5
