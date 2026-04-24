"""
3D Attitude / Pointing Scene Widget (pyqtgraph OpenGL).

M4 升级内容：
- 默认机体改为**扁平长方体**（相控阵卫通终端外形），+X 机头方向
  未来可通过 `set_body_model(mesh_data)` 接入具体设备的 STL/OBJ 模型
- 在机体坐标系内绘制：
  · 卫星矢量（红）—— 由 `tgt_az/tgt_el` 通道算出
  · 天线实际法向（绿）—— 由 `ant_az/ant_el` 通道算出
  · 误差夹角扇面（半透明黄）—— 两矢量间插值三角形
  · 扫描轨迹（淡蓝）—— 最近 N 个 ant_az/el 历史点
- 7 路通道绑定：roll/pitch/yaw/tgt_az/tgt_el/ant_az/ant_el
  auto_bind_from_profile 扩展到这 7 路

**坐标系约定**（相控阵波束坐标系 → 机体坐标系）：
- 机体系：+X 机头 / +Y 左翼 / +Z 天顶
- az = 0° 对应机头右方向（-Y），从 +Z 俯视**逆时针**增大
- el = 0° 指天顶（+Z），90° 指设备水平面（XY）

**2026-04-21 简化**：去掉 tgt_az/tgt_el 目标卫星矢量 + 误差扇面 —— 下位机实际
只上报实时波束指向（ant_az/ant_el），没有"目标方向"独立通道。只保留：
  - 天线实际法向（绿线）= 当前 ant_az/ant_el
  - 扫描轨迹（淡蓝点迹）= 历史 ant 方向
若未来有目标方向需求可把 tgt_* 相关代码加回（保留在 git history）。
- 转换到单位矢量：
    phi   = radians(AZ_PHI_OFFSET_DEG + AZ_SIGN * az)
    theta = radians(el)
    x = sin(theta) * cos(phi)
    y = sin(theta) * sin(phi)
    z = cos(theta)
  若设备实际方向相反，只改下面两个常数即可。
"""
from typing import Optional
from collections import deque
import numpy as np

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
)
from PySide6.QtCore import Qt, QTimer, Signal
import pyqtgraph.opengl as gl
from pyqtgraph.opengl import GLViewWidget, GLLinePlotItem, GLMeshItem, MeshData

from satellite_debug_tool.ui import styles as S


# ------ 坐标系常数（见文件头注释） ------
AZ_PHI_OFFSET_DEG = 270.0    # az=0 对应 -Y（机头右），phi = 270° + az
AZ_SIGN = +1                 # +1 = az 增大时逆时针；-1 翻转为顺时针
R_POINTING = 4.0             # 卫星/天线矢量画到球面半径
SCAN_TRAIL_LEN = 300         # 扫描轨迹最长保留点数


def _pointing_unit_vec(az_deg: float, el_deg: float) -> np.ndarray:
    """az/el (deg) → 机体系单位矢量 (3,) float32。"""
    phi = np.radians(AZ_PHI_OFFSET_DEG + AZ_SIGN * az_deg)
    theta = np.radians(el_deg)
    st = np.sin(theta)
    return np.array(
        [st * np.cos(phi), st * np.sin(phi), np.cos(theta)],
        dtype=np.float32,
    )


def _build_error_fan(
    v_a: np.ndarray, v_b: np.ndarray, segments: int = 12,
) -> tuple[np.ndarray, np.ndarray]:
    """构造从原点到 v_a/v_b 两矢量之间的三角扇（球面插值）。

    返回 (vertices (N+2, 3) float32, faces (N, 3) uint32)。
    segments 越大扇面越光滑；同向/反向/零矢量时退化为空。
    """
    segments = max(2, int(segments))
    va = np.asarray(v_a, dtype=np.float64)
    vb = np.asarray(v_b, dtype=np.float64)
    na = np.linalg.norm(va)
    nb = np.linalg.norm(vb)
    if na < 1e-6 or nb < 1e-6:
        return (
            np.empty((0, 3), dtype=np.float32),
            np.empty((0, 3), dtype=np.uint32),
        )
    ua = va / na
    ub = vb / nb
    dot = float(np.clip(np.dot(ua, ub), -1.0, 1.0))
    # 几乎同向：没有可见扇面
    if dot > 0.9999:
        return (
            np.empty((0, 3), dtype=np.float32),
            np.empty((0, 3), dtype=np.uint32),
        )
    r = 0.5 * (na + nb)                       # 扇面半径取两矢量平均长度
    omega = np.arccos(dot)
    sin_omega = np.sin(omega)

    # 球面线性插值（slerp），生成 segments+1 个点
    ts = np.linspace(0.0, 1.0, segments + 1)
    if sin_omega < 1e-6:
        # 近反向：退化为线性插值避免 0/0
        arc = (1 - ts)[:, None] * ua + ts[:, None] * ub
    else:
        s0 = np.sin((1 - ts) * omega) / sin_omega
        s1 = np.sin(ts * omega) / sin_omega
        arc = s0[:, None] * ua + s1[:, None] * ub
    arc *= r

    # 顶点：原点 + 弧上 segments+1 个点
    verts = np.vstack([[0.0, 0.0, 0.0], arc]).astype(np.float32)
    # 面：扇形三角 (0, i, i+1)
    faces = np.empty((segments, 3), dtype=np.uint32)
    faces[:, 0] = 0
    faces[:, 1] = np.arange(1, segments + 1)
    faces[:, 2] = np.arange(2, segments + 2)
    return verts, faces


class AttitudeWidget(QWidget):
    """
    3D attitude indicator widget.

    Displays a 3D aircraft model that rotates in real-time based on
    Roll, Pitch, and Yaw values from selected data channels.
    """

    channel_changed = Signal(str, str)  # (channel_name, axis)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._roll_ch = ""
        self._pitch_ch = ""
        self._yaw_ch = ""
        self._roll_value = 0.0
        self._pitch_value = 0.0
        self._yaw_value = 0.0
        self._is_dark = True
        self._theme = "dark"
        self._scale = "medium"
        # 2026-04-21：combo 已全部移除，_selector_labels 不再需要
        self._value_name_labels: list[QLabel] = []

        # M4: 指向通道（简化后只保留 ant_*）
        self._ant_az_ch = ""
        self._ant_el_ch = ""
        self._ant_az_value = 0.0
        self._ant_el_value = 0.0
        self._have_ant = False
        # 保留字段名为了向后兼容 settings.json 里的老 key，但值恒为 False
        self._tgt_az_ch = ""
        self._tgt_el_ch = ""
        self._have_tgt = False
        # 扫描轨迹（机体系点，每帧加一个）
        self._scan_trail: deque = deque(maxlen=SCAN_TRAIL_LEN)

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 2026-04-21：移除 5 路通道绑定 combo。所有通道名 → ch_id 的映射
        # 通过 auto_bind_from_profile() 按 profile 通道名自动完成，用户无需手动选。
        # 只保留"复位视角"按钮一行。
        topbar = QWidget()
        topbar_layout = QHBoxLayout(topbar)
        topbar_layout.setContentsMargins(0, 0, 0, 0)
        topbar_layout.setSpacing(8)
        topbar_layout.addStretch(1)
        self._reset_view_btn = QPushButton("复位视角")
        self._reset_view_btn.setToolTip(
            "把 3D 相机恢复到默认角度（distance=10 / elev=30 / azim=45）"
        )
        self._reset_view_btn.clicked.connect(self._reset_view)
        topbar_layout.addWidget(self._reset_view_btn)
        layout.addWidget(topbar)

        # ---- 3D OpenGL view ----
        self._gl_view = GLViewWidget()
        self._gl_view.setCameraPosition(distance=10, elevation=30, azimuth=45)
        self._gl_view.setBackgroundColor(0x1E, 0x1E, 0x1E)

        # 栅格
        grid = gl.GLGridItem()
        grid.setSize(20, 20)
        grid.setSpacing(1, 1)
        grid.setColor((0x3C, 0x3C, 0x3C, 0.8))
        self._gl_view.addItem(grid)

        # 参考轴（body frame）
        self._axes = self._create_axes()
        for ax in self._axes.values():
            self._gl_view.addItem(ax)

        # 机体模型（默认长方体，未来可 set_body_model 替换）
        self._body = self._create_aircraft()
        self._gl_view.addItem(self._body)
        # 兼容旧引用名（update_attitude 仍用 self._aircraft 变换）
        self._aircraft = self._body

        # 机头方向指示箭头（红色小四棱锥，与机体一起旋转）
        self._nose_arrow = self._create_nose_arrow()
        self._gl_view.addItem(self._nose_arrow)

        # M4: 实时波束矢量（绿）+ 扫描轨迹（淡蓝）。已去掉 tgt 卫星矢量 + 误差扇面。
        empty2 = np.empty((0, 3), dtype=np.float32)
        self._ant_line = GLLinePlotItem(pos=empty2, color=(0.30, 0.95, 0.50, 1.0), width=2.5)
        self._trail_line = GLLinePlotItem(
            pos=empty2, color=(0.50, 0.80, 1.0, 0.70), width=1.5,
        )
        for it in (self._ant_line, self._trail_line):
            self._gl_view.addItem(it)
        # 占位空对象，update_pointing / clear 仍会引用；不加到 3D 视图
        self._tgt_line = GLLinePlotItem(pos=empty2, color=(0, 0, 0, 0), width=0)
        self._err_mesh = GLMeshItem(
            vertexes=empty2, faces=np.empty((0, 3), dtype=np.uint32),
            smooth=False, drawEdges=False, color=(0, 0, 0, 0),
        )

        layout.addWidget(self._gl_view, stretch=1)

        # Values display row
        values_widget = QWidget()
        values_layout = QHBoxLayout(values_widget)
        values_layout.setContentsMargins(0, 0, 0, 0)
        for label_text, attr in [
            ("Roll:", "_roll_value"),
            ("Pitch:", "_pitch_value"),
            ("Yaw:", "_yaw_value"),
        ]:
            lbl = QLabel(label_text)
            self._value_name_labels.append(lbl)
            val_lbl = QLabel("0.0°")
            values_layout.addWidget(lbl)
            values_layout.addWidget(val_lbl)
            if attr == "_roll_value":
                self._roll_val_lbl = val_lbl
            elif attr == "_pitch_value":
                self._pitch_val_lbl = val_lbl
            else:
                self._yaw_val_lbl = val_lbl
        values_layout.addStretch()
        layout.addWidget(values_widget)
        self._apply_label_styles()

    def _create_axes(self) -> dict:
        """Create X, Y, Z reference axes."""
        # X axis (red) - forward
        x_data = np.array([[0, 0, 0], [3, 0, 0]], dtype=np.float32)
        x_line = GLLinePlotItem(pos=x_data, color=(1, 0, 0, 1), width=2)
        # Y axis (green) - right
        y_data = np.array([[0, 0, 0], [0, 3, 0]], dtype=np.float32)
        y_line = GLLinePlotItem(pos=y_data, color=(0, 1, 0, 1), width=2)
        # Z axis (blue) - down
        z_data = np.array([[0, 0, 0], [0, 0, 3]], dtype=np.float32)
        z_line = GLLinePlotItem(pos=z_data, color=(0, 0, 1, 1), width=2)
        return {"x": x_line, "y": y_line, "z": z_line}

    def _create_aircraft(self) -> GLMeshItem:
        """默认机体模型：带明显楔形机头的扁平长方体（相控阵卫通终端外形）。

        尺寸 4.0 × 1.4 × 0.3（长 × 宽 × 高），长宽比约 2.9:1 让"长边"一眼可辨。
        机头方向沿 +X：前 40% 长度是楔形（顶面斜削到底面前缘），形成明显船头状。
        机头再配一个红色箭头指示器（见 `_create_nose_arrow`），杜绝朝向歧义。

        未来通过 `set_body_model(mesh_data)` 接入具体设备的实测模型。
        """
        L, W, H = 2.0, 0.7, 0.15   # 半长 / 半宽 / 半高
        nose_len = L * 0.4         # 机头楔形长度（占半长 40%，整机占 20%）

        # 8 顶点：底面 4 角完整矩形 + 顶面 4 角（机头端向后缩 nose_len）
        verts = np.array([
            # 底面（z=-H）
            [-L, +W, -H],              # 0 tail_left_bottom
            [-L, -W, -H],              # 1 tail_right_bottom
            [+L, -W, -H],              # 2 nose_right_bottom（机头尖端右角）
            [+L, +W, -H],              # 3 nose_left_bottom（机头尖端左角）
            # 顶面（z=+H），机头端只到 x=+L-nose_len
            [-L, +W, +H],              # 4 tail_left_top
            [-L, -W, +H],              # 5 tail_right_top
            [+L - nose_len, -W, +H],   # 6 nose_right_top（楔形折线点）
            [+L - nose_len, +W, +H],   # 7 nose_left_top（楔形折线点）
        ], dtype=np.float32)

        # 三角面（顶点顺序遵循右手法则，法线朝外）
        faces = np.array([
            # 底面 (法线 -Z)
            [0, 3, 2], [0, 2, 1],
            # 顶面（被机头楔形截短，法线 +Z）
            [4, 5, 6], [4, 6, 7],
            # 机尾面 (-X, 法线 -X)
            [0, 1, 5], [0, 5, 4],
            # 左侧面 (+Y, 四边形梯形)
            [0, 4, 7], [0, 7, 3],
            # 右侧面 (-Y, 四边形梯形)
            [1, 2, 6], [1, 6, 5],
            # 机头楔形斜面：从顶面 nose_* 斜下到底面 +X 边
            [7, 6, 2], [7, 2, 3],
        ], dtype=np.uint32)

        md = MeshData(vertexes=verts, faces=faces)
        mesh = GLMeshItem(
            meshdata=md,
            smooth=False,
            drawEdges=True,
            edgeColor=(0.45, 0.55, 0.75, 1),
        )
        mesh.setColor((0.35, 0.45, 0.65, 0.95))
        return mesh

    def _create_nose_arrow(self) -> GLMeshItem:
        """机头方向指示箭头：红色小四棱锥，底面在 +X 端外侧，尖端沿 +X 指。

        与 aircraft 共享 setTransform（即随姿态一起旋转），永远指示机头方向。
        尺寸相对机体很小（长约 0.35，宽 0.25），避免视觉喧宾夺主。
        """
        # 机头（+X）端顶点位置
        nose_x = 2.0  # 对齐 _create_aircraft 的 L
        base = nose_x + 0.05     # 箭头基面 x
        tip = base + 0.45        # 箭头尖端 x
        half = 0.18              # 基面半宽/半高

        verts = np.array([
            [tip, 0.0, 0.0],       # 0 tip 尖端
            [base, -half, -half],  # 1 base_right_bottom
            [base, +half, -half],  # 2 base_left_bottom
            [base, +half, +half],  # 3 base_left_top
            [base, -half, +half],  # 4 base_right_top
        ], dtype=np.float32)

        faces = np.array([
            # 底面（朝 -X，四边形 → 2 三角）
            [1, 2, 3], [1, 3, 4],
            # 4 个侧三角收敛到尖端（法线朝外）
            [0, 2, 1],   # 下面
            [0, 3, 2],   # 左面
            [0, 4, 3],   # 上面
            [0, 1, 4],   # 右面
        ], dtype=np.uint32)

        md = MeshData(vertexes=verts, faces=faces)
        mesh = GLMeshItem(
            meshdata=md,
            smooth=False,
            drawEdges=True,
            edgeColor=(1.0, 0.3, 0.3, 1.0),
        )
        mesh.setColor((0.95, 0.20, 0.20, 0.95))   # 醒目的红
        return mesh

    def set_body_model(self, mesh_data: MeshData) -> None:
        """替换默认长方体为自定义 3D 模型（未来接设备 STL/OBJ 用）。

        调用方负责保证 mesh_data 顶点单位与默认模型一致（长边 ~3 单位）。
        """
        if self._body is not None:
            self._gl_view.removeItem(self._body)
        self._body = GLMeshItem(
            meshdata=mesh_data,
            smooth=False,
            drawEdges=True,
            edgeColor=(0.45, 0.55, 0.75, 1),
        )
        self._body.setColor((0.35, 0.45, 0.65, 0.95))
        self._gl_view.addItem(self._body)

    def set_channel_options(self, names: list[str]) -> None:
        """旧 API：曾用于刷新 combo 候选项。2026-04-21 combo 已移除，
        保留签名为 no-op，防止外部调用报错。"""
        _ = names

    # 每个 axis 的匹配模式：(exact_names, contain_patterns)
    # exact：profile 里完全等于这些名字的优先命中；contain：小写 contains 兜底
    _AUTO_BIND_RULES = {
        "roll":   (("roll",),                      ("roll",)),
        "pitch":  (("pitch",),                     ("pitch",)),
        "yaw":    (("yaw", "heading"),             ("yaw",)),
        "ant_az": (("ant_az", "antenna_az"),       ("ant_az", "antenna_az")),
        "ant_el": (("ant_el", "antenna_el"),       ("ant_el", "antenna_el")),
    }

    def auto_bind_from_profile(self, channel_name_to_key: dict[str, str]) -> None:
        """按 profile 通道名自动绑定 5 路（姿态 + 波束指向）。

        2026-04-21：UI 不再提供手动选择，本函数直接把结果写到 `_{axis}_ch` 字段。
        MainWindow.update_heavy 会按这几个字段查 DataStore 实际值。

        Args:
            channel_name_to_key: 小写 profile 通道名 → DataStore 内部 key
                （例如 {"roll": "ch_00", "ant_az": "ch_03", ...}）。
        """
        picks: dict[str, str] = {}
        for axis, (exacts, contains) in self._AUTO_BIND_RULES.items():
            hit = None
            for e in exacts:
                if e in channel_name_to_key:
                    hit = channel_name_to_key[e]
                    break
            if hit is None:
                for name_lower, key in channel_name_to_key.items():
                    if any(p in name_lower for p in contains):
                        hit = key
                        break
            if hit:
                picks[axis] = hit

        # 直接写字段（不走 combo / 信号 / settings 持久化）
        self._roll_ch   = picks.get("roll",   "")
        self._pitch_ch  = picks.get("pitch",  "")
        self._yaw_ch    = picks.get("yaw",    "")
        self._ant_az_ch = picks.get("ant_az", "")
        self._ant_el_ch = picks.get("ant_el", "")
        self._have_ant  = bool(self._ant_az_ch and self._ant_el_ch)
        if not self._have_ant:
            # 切设备或通道缺失时清掉残留扫描轨迹
            self._scan_trail.clear()
            self._trail_line.setData(pos=np.empty((0, 3), dtype=np.float32))

    def update_attitude(
        self,
        roll: float,
        pitch: float,
        yaw: float,
        roll_ch: str,
        pitch_ch: str,
        yaw_ch: str,
    ) -> None:
        """
        Update the 3D attitude display with new angle values.

        Args:
            roll: Roll angle in degrees
            pitch: Pitch angle in degrees
            yaw: Yaw angle in degrees
            roll_ch: Name of the roll channel
            pitch_ch: Name of the pitch channel
            yaw_ch: Name of the yaw channel
        """
        self._roll_value = roll
        self._pitch_value = pitch
        self._yaw_value = yaw
        self._roll_ch = roll_ch
        self._pitch_ch = pitch_ch
        self._yaw_ch = yaw_ch

        # Convert to radians for rotation matrix
        r = np.radians(roll)
        p = np.radians(pitch)
        y = np.radians(yaw)

        # Rotation matrix (Z-Y-X Euler angles: yaw, pitch, roll)
        # R = Rz(yaw) * Ry(pitch) * Rx(roll)
        cos_r, sin_r = np.cos(r), np.sin(r)
        cos_p, sin_p = np.cos(p), np.sin(p)
        cos_y, sin_y = np.cos(y), np.sin(y)

        # Rotation matrix elements
        r00 = cos_y * cos_p
        r01 = cos_y * sin_p * sin_r - sin_y * cos_r
        r02 = cos_y * sin_p * cos_r + sin_y * sin_r

        r10 = sin_y * cos_p
        r11 = sin_y * sin_p * sin_r + cos_y * cos_r
        r12 = sin_y * sin_p * cos_r - cos_y * sin_r

        r20 = -sin_p
        r21 = cos_p * sin_r
        r22 = cos_p * cos_r

        # Build 4x4 transformation matrix (rotation only, no translation)
        transform = np.array(
            [[r00, r01, r02, 0],
             [r10, r11, r12, 0],
             [r20, r21, r22, 0],
             [0,   0,   0,   1]], dtype=np.float32
        )
        self._aircraft.setTransform(transform)
        # 机头箭头与机体共享变换，始终指示当前机头方向
        self._nose_arrow.setTransform(transform)

        # Update value labels
        self._roll_val_lbl.setText(f"{roll:.1f}°")
        self._pitch_val_lbl.setText(f"{pitch:.1f}°")
        self._yaw_val_lbl.setText(f"{yaw:.1f}°")
        # 2026-04-21：combo 已删除，不再需要高亮样式

    def get_channel_selections(self) -> tuple[str, str, str]:
        """返回 (roll, pitch, yaw) 当前绑定的 DataStore key。
        2026-04-21：由 auto_bind_from_profile 设置，用户无法手动覆盖。"""
        return (self._roll_ch, self._pitch_ch, self._yaw_ch)

    def get_pointing_selections(self) -> tuple[str, str, str, str]:
        """返回 (tgt_az, tgt_el, ant_az, ant_el)。tgt_* 已移除恒为空字符串。"""
        return ("", "", self._ant_az_ch, self._ant_el_ch)

    # ---------- M4: 指向 / 视角 / 轨迹 ----------

    def _reset_view(self) -> None:
        """把 3D 相机恢复到默认角度。"""
        self._gl_view.setCameraPosition(distance=10, elevation=30, azimuth=45)

    def update_pointing(
        self,
        tgt_az: Optional[float],
        tgt_el: Optional[float],
        ant_az: Optional[float],
        ant_el: Optional[float],
    ) -> None:
        """由 MainWindow 每帧调用。tgt_* 参数已弃用（恒为 None），仅保留签名兼容。"""
        _ = tgt_az, tgt_el  # 显式吃掉未使用参数，避免 linter 警告
        empty2 = np.empty((0, 3), dtype=np.float32)

        # ---- 天线法向（绿） + 扫描轨迹 ----
        if self._have_ant and ant_az is not None and ant_el is not None:
            self._ant_az_value = float(ant_az)
            self._ant_el_value = float(ant_el)
            v_ant = _pointing_unit_vec(ant_az, ant_el) * R_POINTING
            self._ant_line.setData(
                pos=np.array([[0, 0, 0], v_ant], dtype=np.float32)
            )
            self._scan_trail.append(v_ant.copy())
            if len(self._scan_trail) >= 2:
                self._trail_line.setData(
                    pos=np.asarray(self._scan_trail, dtype=np.float32)
                )
            else:
                self._trail_line.setData(pos=empty2)
        else:
            self._ant_line.setData(pos=empty2)
        # 2026-04-21: combo 已移除，不再需要高亮

    # ---------- 主题 / 字号 (M6) ----------

    def _apply_label_styles(self) -> None:
        p = S.palette(self._theme)
        selector_px = S.font_px(11, self._scale)
        value_px = S.font_px(10, self._scale)
        for lbl in self._value_name_labels:
            lbl.setStyleSheet(f"color: {p['text_muted']}; font-size: {value_px}px;")
        for val_lbl in (self._roll_val_lbl, self._pitch_val_lbl, self._yaw_val_lbl):
            val_lbl.setStyleSheet(
                f"color: {p['text']}; font-size: {value_px}px; min-width: 50px;"
            )
        # M6: 复位视角按钮样式跟随主题
        self._reset_view_btn.setStyleSheet(
            f"QPushButton {{ background-color: {p['input_bg']}; color: {p['text']}; "
            f"border: 1px solid {p['input_border']}; border-radius: 2px; "
            f"padding: 2px 8px; font-size: {selector_px}px; }}"
            f"QPushButton:hover {{ background-color: {p['card_alt']}; }}"
        )

    def set_dark_theme(self, is_dark: bool) -> None:
        self.set_theme("dark" if is_dark else "light", self._scale)

    def set_theme(self, theme: str = "dark", scale: str = "medium") -> None:
        self._theme = S._normalize_theme(theme)
        self._is_dark = self._theme != "light"
        self._scale = scale
        # 3D 视图背景
        if self._theme == "dark_hc":
            self._gl_view.setBackgroundColor(0x00, 0x00, 0x00)
        elif self._theme == "light":
            self._gl_view.setBackgroundColor(0xFF, 0xFF, 0xFF)
        else:
            self._gl_view.setBackgroundColor(0x1E, 0x1E, 0x1E)
        self._apply_label_styles()

    def clear(self) -> None:
        """Reset the body to default orientation and clear pointing scene."""
        self._roll_value = 0.0
        self._pitch_value = 0.0
        self._yaw_value = 0.0
        self._aircraft.setTransform(np.eye(3, dtype=np.float32))
        self._nose_arrow.setTransform(np.eye(3, dtype=np.float32))
        self._roll_val_lbl.setText("0.0°")
        self._pitch_val_lbl.setText("0.0°")
        self._yaw_val_lbl.setText("0.0°")
        # M4: 清指向矢量 / 扫描轨迹
        empty2 = np.empty((0, 3), dtype=np.float32)
        self._ant_line.setData(pos=empty2)
        self._trail_line.setData(pos=empty2)
        self._scan_trail.clear()
        self._ant_az_value = self._ant_el_value = 0.0
