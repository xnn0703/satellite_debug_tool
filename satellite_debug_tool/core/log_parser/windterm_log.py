"""WindTerm 控制台 log 解析器（M7-S6）。

解析下位机通过控制台周期打印的"表格行"日志，自动识别表头、提取数字列、
按行号占位时间戳。供 LogView 灌入虚拟 ProfileStore + DataStore，复用
GroupedChartWidget 绘制曲线。

输入格式（用户实测样例）::

    [2026-05-14 19:29:57] I(0) track_debug_print_table_header: state hcal gps yaw pitch roll ...
    [2026-05-14 19:29:57] I(0) track_table_row_bynav: GLB 0 0 +357.61 +0.03 +0.46 ...
    [2026-05-14 19:29:58] I(0) track_table_row_bynav: GLB 0 0 +357.61 +0.03 +0.46 ...
    ...

规则：

- 表头行：``track_debug_print_table_header:`` 后空格分隔的列名；如多次出现取**最近一次**
- 数据行：``track_table_row_bynav:`` 后空格分隔的值，列数应与表头一致；不一致跳过并累计 warning
- **非数字列自动剔除**：扫描首条有效数据行，每列尝试 ``float()``，
  失败列从结果中整列丢弃（``state``/``ins_st``/``ALGN`` 等枚举字符串）
- **时间戳**：M7 阶段忽略行首 ``[YYYY-MM-DD HH:MM:SS]``，按行号 × 100ms
  生成等距占位（下位机将来吐 ms 时间戳后只需替换 ``_timestamp_for_row``）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Union

import numpy as np


_HEADER_RE = re.compile(r"track_debug_print_table_header:\s*(.+?)\s*$")
_ROW_RE = re.compile(r"track_table_row_bynav:\s*(.+?)\s*$")

# 行号 → 占位时间戳（毫秒）的转换函数。未来切到下位机 ms 时间戳只需替换此函数。
_DEFAULT_ROW_TO_MS = lambda row_idx: int(row_idx * 100)


@dataclass
class WindTermLogResult:
    """解析结果。

    Attributes:
        columns: 已剔除非数字列后的列名列表
        data:    shape=(N, M) 二维浮点矩阵，N 行对应原数据行数，M = len(columns)
        timestamps_ms: shape=(N,) 占位时间戳（毫秒）
        skipped_rows: 跳过的行数（列数错位等）
        total_data_rows: 原始 track_table_row_bynav 行总数（含被跳过的）
    """

    columns: List[str]
    data: np.ndarray
    timestamps_ms: np.ndarray
    skipped_rows: int = 0
    total_data_rows: int = 0
    dropped_non_numeric_cols: List[str] = field(default_factory=list)


class WindTermLogParser:
    """无状态 parser。一次性读全文件。"""

    @staticmethod
    def parse(
        filepath: Union[str, Path],
        progress_cb: Optional[Callable[[int], None]] = None,
        progress_every: int = 5000,
    ) -> WindTermLogResult:
        path = Path(filepath)
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return WindTermLogParser._parse_lines(f, progress_cb, progress_every)

    @staticmethod
    def parse_text(text: str) -> WindTermLogResult:
        """同 parse，但输入是字符串（便于单测）。"""
        return WindTermLogParser._parse_lines(text.splitlines(), None, 5000)

    # --------- 内部 ---------

    @staticmethod
    def _parse_lines(
        lines,
        progress_cb: Optional[Callable[[int], None]],
        progress_every: int,
    ) -> WindTermLogResult:
        header_cols: Optional[List[str]] = None
        raw_rows: List[List[str]] = []
        total_data_rows = 0
        line_count = 0

        for raw_line in lines:
            line_count += 1
            if progress_cb is not None and line_count % progress_every == 0:
                progress_cb(line_count)

            hm = _HEADER_RE.search(raw_line)
            if hm:
                header_cols = hm.group(1).split()
                continue

            rm = _ROW_RE.search(raw_line)
            if rm:
                total_data_rows += 1
                raw_rows.append(rm.group(1).split())

        if header_cols is None:
            return WindTermLogResult(
                columns=[],
                data=np.empty((0, 0), dtype=np.float32),
                timestamps_ms=np.empty((0,), dtype=np.float64),
                skipped_rows=total_data_rows,
                total_data_rows=total_data_rows,
                dropped_non_numeric_cols=[],
            )

        ncols_expected = len(header_cols)

        # 1) 按列数对齐过滤
        aligned: List[List[str]] = []
        skipped = 0
        for row in raw_rows:
            if len(row) == ncols_expected:
                aligned.append(row)
            else:
                skipped += 1

        if not aligned:
            return WindTermLogResult(
                columns=[],
                data=np.empty((0, 0), dtype=np.float32),
                timestamps_ms=np.empty((0,), dtype=np.float64),
                skipped_rows=skipped,
                total_data_rows=total_data_rows,
                dropped_non_numeric_cols=[],
            )

        # 2) 扫描首条数据行，识别哪些列能 float()。失败列整列剔除
        sample_row = aligned[0]
        numeric_mask: List[bool] = []
        for v in sample_row:
            numeric_mask.append(_is_float(v))

        dropped_cols = [
            name for name, is_num in zip(header_cols, numeric_mask) if not is_num
        ]
        kept_cols = [
            name for name, is_num in zip(header_cols, numeric_mask) if is_num
        ]
        kept_indices = [i for i, ok in enumerate(numeric_mask) if ok]

        if not kept_cols:
            return WindTermLogResult(
                columns=[],
                data=np.empty((0, 0), dtype=np.float32),
                timestamps_ms=np.empty((0,), dtype=np.float64),
                skipped_rows=skipped,
                total_data_rows=total_data_rows,
                dropped_non_numeric_cols=dropped_cols,
            )

        # 3) 把数据行转成 ndarray；行内 float() 解析失败的整行跳过
        data_matrix: List[List[float]] = []
        for row in aligned:
            try:
                parsed = [float(row[i]) for i in kept_indices]
            except ValueError:
                skipped += 1
                continue
            data_matrix.append(parsed)

        data_array = np.asarray(data_matrix, dtype=np.float32)
        timestamps = np.asarray(
            [_DEFAULT_ROW_TO_MS(i) for i in range(len(data_matrix))],
            dtype=np.float64,
        )

        return WindTermLogResult(
            columns=kept_cols,
            data=data_array,
            timestamps_ms=timestamps,
            skipped_rows=skipped,
            total_data_rows=total_data_rows,
            dropped_non_numeric_cols=dropped_cols,
        )


def _is_float(s: str) -> bool:
    """带符号、含小数、科学计数法均算数字。'+1.23', '-1e-5', '0' 都 True。"""
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False
