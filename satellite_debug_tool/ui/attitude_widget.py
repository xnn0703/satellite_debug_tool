"""
3D Attitude / Pointing Scene Widget (pyqtgraph OpenGL).

**2026-05-25 几何重写**：完全反向设备端 beampointing 计算链，让 widget 显示
绝对空间方向（GEO 卫星跟踪锁定时，波束端点应稳定在固定空间点附近）。

═══════════════════════════════════════════════════════════════════════════
坐标系约定
═══════════════════════════════════════════════════════════════════════════
- **widget 视觉世界 = NWU**：+X 北 / +Y 西 / +Z 上（右手系，+Z 朝上直观显示）
- **设备 body = FRD**：     +X 前 / +Y 右 / +Z 下（航空标准，与下位机一致）
- **设备 geo  = NED**：     +X 北 / +Y 东 / +Z 下（航空/航海标准）
- **mesh 模型 = FLU**：     +X 前 / +Y 左 / +Z 上（pyqtgraph 友好，+Z 朝上）

各坐标系间映射：
    P = diag(1, -1, -1)            # Y, Z 翻号，det=+1 保持右手系
    NED ↔ NWU： v_world = P · v_geo
    FRD ↔ FLU： v_mesh  = P · v_body

载体姿态 DCM（与下位机 bp_dcm_body_to_geo 一致）：
    R_body2geo = Rz(yaw) · Ry(pitch) · Rx(roll)   # 标准右手 ZYX Tait-Bryan
    yaw   : 绕 body +Z(下) 右手定则，北=0 CW+
    pitch : 绕 body +Y(右) 右手定则，抬头+
    roll  : 绕 body +X(前) 右手定则，右翼下沉+

═══════════════════════════════════════════════════════════════════════════
波束几何反向解链（_ant_to_world_nwu）
═══════════════════════════════════════════════════════════════════════════
设备上报 `ant_az/ant_el` 是阵面驱动角（Layer 3 输出），需依次反向：

  Step 1 — 反 Layer 3 输出规整（afd01 mount）：
      el_up   = 90 - ant_el       # el_is_zenith=true，输入为天顶距
      az_math = -ant_az           # az_ccw_positive=false，硬件 CCW → 数学 CW

  Step 2 — 重建阵面单位向量 v_array：
      v_array = ( cos(el_up)·cos(az_math),
                  cos(el_up)·sin(az_math),
                  -sin(el_up) )

  Step 3 — array → body (FRD)：
      R_array2body = Rz(+90°)            # afd01 mount_yaw=+90°
      v_body = R_array2body · v_array
             = (-v_array.y, v_array.x, v_array.z)

  Step 4 — body → geo (NED)：
      v_geo = R_body2geo · v_body

  Step 5 — geo → world (NWU)：
      v_world = P · v_geo

═══════════════════════════════════════════════════════════════════════════
mesh 姿态变换
═══════════════════════════════════════════════════════════════════════════
mesh 顶点在 FLU 系内构造，要变换到 widget 世界系 NWU：
    p_world_NWU = P · R_body2geo · P · p_mesh_FLU = (P R P) · p_mesh
零姿态时 P·I·P = I，mesh 与世界轴自然对齐（机头朝北 +X，左翼朝西 +Y，顶 +Z）。
"""
from typing import Optional
from collections import deque
import numpy as np

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QVector3D
from pyqtgraph.opengl import GLViewWidget, GLLinePlotItem, GLMeshItem, MeshData

from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S


# ------ 坐标系常数（见文件头注释） ------
R_POINTING = 4.0             # 波束矢量画到球面半径
SCAN_TRAIL_LEN = 300         # 扫描轨迹最长保留点数

# 默认相机：设备位于下半区、正常朝天的波束和轨迹位于上半区。
# 观察中心上移，避免固定半径的扫描轨迹贴住顶部工具栏；不改变波束几何或用户手动视角。
_CAM_DISTANCE = 14.0
_CAM_ELEVATION = 22.0
_CAM_AZIMUTH = 45.0
_CAM_CENTER_Z = 0.9

# NED↔NWU 映射（也用于 FRD↔FLU）：Y,Z 翻号，det=+1 保持右手系
P_NED_NWU = np.diag([1.0, -1.0, -1.0]).astype(np.float64)

# afd01 阵面装机参数（与设备端 bp_mount_afd01_default 同步）：
# mount_yaw=+90° → R_array2body = Rz(+90°)，array +X → body +Y（机头右）
# az_ccw_positive=false：硬件 az_out = -az_math
# el_is_zenith=true：硬件 el_out = 90 - el_up
_MOUNT_YAW_DEG = 90.0


def _default_camera_center() -> QVector3D:
    """返回新的默认观察中心，避免用户拖动相机时改写共享对象。"""
    return QVector3D(0.0, 0.0, _CAM_CENTER_Z)


def _ant_to_world_nwu(
    ant_az_deg: float,
    ant_el_deg: float,
    R_body2geo: np.ndarray,
) -> np.ndarray:
    """阵面驱动角 (ant_az, ant_el) + 姿态 DCM → widget 世界系 (NWU) 单位向量。

    完全反向 Layer 3 + Layer 2 计算链；文件头有详细 5 步说明。返回 float32 (3,)。

    Args:
        ant_az_deg: 设备上报阵面方位角（硬件 CCW+，[0, 360)）
        ant_el_deg: 设备上报阵面仰角（天顶距，0=朝天 90=水平）
        R_body2geo: 当前载体姿态 DCM（3x3 float64），由 update_attitude 维护
    """
    # Step 1 + 2: 反 Layer 3 输出规整，重建阵面单位向量
    el_up = np.radians(90.0 - ant_el_deg)
    az_math = np.radians(-ant_az_deg)
    ce = np.cos(el_up)
    v_array = np.array([ce * np.cos(az_math),
                        ce * np.sin(az_math),
                        -np.sin(el_up)], dtype=np.float64)
    # Step 3: array → body FRD，Rz(+90°)·v = (-v.y, v.x, v.z)
    v_body = np.array([-v_array[1], v_array[0], v_array[2]], dtype=np.float64)
    # Step 4: body FRD → geo NED
    v_geo = R_body2geo @ v_body
    # Step 5: geo NED → world NWU
    v_world = P_NED_NWU @ v_geo
    return v_world.astype(np.float32)


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
        self._yaw_reference = "legacy"
        self._is_dark = True
        self._theme = "dark"
        self._scale = "medium"
        self._readout_emphasis = False
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
        # 扫描轨迹（世界系点，每帧加一个）
        self._scan_trail: deque = deque(maxlen=SCAN_TRAIL_LEN)
        # 机体→世界旋转矩阵（由 update_attitude 维护）
        self._body_rot = np.eye(3, dtype=np.float64)
        # 设备 STL 模型朝向（STL 轴 → widget FLU：X=机头/Y=左翼/Z=天顶）。
        # AFD01/ESA01：Z(厚度52)=天顶，实际机头是原始 +Y；原始 +X 映射到
        # widget -Y，等价于从旧 +X 机头俯视逆时针转 90 度且不镜像模型。
        self._model_up_axis = 2
        self._model_nose_axis = 1
        self._model_nose_sign = 1.0
        self._model_left_sign = -1.0
        self._loaded_model_hw = None
        # 设备 STL 原始 verts/faces（加载后保留，切主题时重新烘焙明暗）
        self._device_verts = None
        self._device_faces = None

        self._setup_ui()
        register_translatable(self)

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
        self._reset_view_btn = QPushButton(tr("Reset view"))
        self._reset_view_btn.setToolTip(
            tr(
                "Restore the 3D camera to its default angle "
                "(distance={distance:g} / elevation={elevation:g} / azimuth={azimuth:g})",
                distance=_CAM_DISTANCE,
                elevation=_CAM_ELEVATION,
                azimuth=_CAM_AZIMUTH,
            )
        )
        self._reset_view_btn.clicked.connect(self._reset_view)
        topbar_layout.addWidget(self._reset_view_btn)
        layout.addWidget(topbar)

        # ---- 3D OpenGL view ----
        self._gl_view = GLViewWidget()
        self._set_default_camera()
        self._gl_view.setBackgroundColor(0x1E, 0x1E, 0x1E)

        # 地面参考网格已移除：z=0 网格平面会横穿居中的设备模型，网格线在穿出
        # 模型边缘处显示成白色斜纹（很扎眼），且对姿态判读帮助不大。保留 None
        # 占位让 _apply_scene_colors 的 grid 分支安全跳过。
        self._grid = None

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

        # 实时波束矢量（绿）+ 扫描轨迹（淡蓝），已反向解链到 widget 世界系 (NWU)
        empty2 = np.empty((0, 3), dtype=np.float32)
        # antialias=True：macOS GL 常把 width>1 截成 1px，开抗锯齿能让粗细生效，
        # 否则浅色背景下细线很淡看不清。
        self._ant_line = GLLinePlotItem(
            pos=empty2, color=(0.30, 0.95, 0.50, 1.0), width=3.5, antialias=True
        )
        self._trail_line = GLLinePlotItem(
            pos=empty2, color=(0.50, 0.80, 1.0, 0.70), width=2.0, antialias=True
        )
        for it in (self._ant_line, self._trail_line):
            self._gl_view.addItem(it)

        # 场景元素（grid/beam/trail/axes）颜色按当前主题刷一遍
        self._apply_scene_colors()

        layout.addWidget(self._gl_view, stretch=1)

        # Values display row
        values_widget = QWidget()
        values_layout = QHBoxLayout(values_widget)
        values_layout.setContentsMargins(0, 0, 0, 0)
        for label_text, attr in [
            (tr("Roll:"), "_roll_value"),
            (tr("Pitch:"), "_pitch_value"),
            (tr("Yaw:"), "_yaw_value"),
        ]:
            lbl = QLabel(label_text)
            self._value_name_labels.append(lbl)
            val_lbl = QLabel("0.0°")
            values_layout.addWidget(lbl)
            values_layout.addWidget(val_lbl)
            if attr == "_roll_value":
                self._roll_name_lbl = lbl
                self._roll_val_lbl = val_lbl
            elif attr == "_pitch_value":
                self._pitch_name_lbl = lbl
                self._pitch_val_lbl = val_lbl
            else:
                self._yaw_name_lbl = lbl
                self._yaw_val_lbl = val_lbl
        values_layout.addStretch()
        layout.addWidget(values_widget)
        self._apply_label_styles()

    def _create_axes(self) -> dict:
        """世界系（NWU）参考轴：红 = +X 北 / 绿 = +Y 西 / 蓝 = +Z 上。"""
        x_data = np.array([[0, 0, 0], [3, 0, 0]], dtype=np.float32)  # +X 北
        x_line = GLLinePlotItem(pos=x_data, color=(1, 0, 0, 1), width=2)
        y_data = np.array([[0, 0, 0], [0, 3, 0]], dtype=np.float32)  # +Y 西
        y_line = GLLinePlotItem(pos=y_data, color=(0, 1, 0, 1), width=2)
        z_data = np.array([[0, 0, 0], [0, 0, 3]], dtype=np.float32)  # +Z 上
        z_line = GLLinePlotItem(pos=z_data, color=(0, 0, 1, 1), width=2)
        return {"x": x_line, "y": y_line, "z": z_line}

    def _create_aircraft(self) -> GLMeshItem:
        """默认机体模型：扁平矩形板（相控阵卫通终端外形）。

        实物形态：左右长、前后短、上下扁的相控阵面板。
        - X（前后）= 短边：半长 0.6 → 全长 1.2（机头方向）
        - Y（左右）= 长边：半宽 1.8 → 全宽 3.6（左翼为 +Y）
        - Z（上下）= 厚度：半高 0.08 → 全厚 0.16
        长宽比约 3:1，与用户示意图一致。机头朝向通过红色箭头单独标识
        （见 `_create_nose_arrow`），不再在 mesh 上做楔形。
        """
        L, W, H = 0.6, 1.8, 0.08
        verts = np.array([
            # 底面（z=-H）
            [-L, +W, -H],   # 0 tail_left_bottom
            [-L, -W, -H],   # 1 tail_right_bottom
            [+L, -W, -H],   # 2 nose_right_bottom
            [+L, +W, -H],   # 3 nose_left_bottom
            # 顶面（z=+H）
            [-L, +W, +H],   # 4 tail_left_top
            [-L, -W, +H],   # 5 tail_right_top
            [+L, -W, +H],   # 6 nose_right_top
            [+L, +W, +H],   # 7 nose_left_top
        ], dtype=np.float32)

        faces = np.array([
            # 底面 (法线 -Z)
            [0, 3, 2], [0, 2, 1],
            # 顶面 (法线 +Z)
            [4, 5, 6], [4, 6, 7],
            # 机尾 (-X)
            [0, 1, 5], [0, 5, 4],
            # 左侧 (+Y)
            [0, 4, 7], [0, 7, 3],
            # 右侧 (-Y)
            [1, 2, 6], [1, 6, 5],
            # 机头 (+X)
            [3, 7, 6], [3, 6, 2],
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
        相控阵面板很宽（Y ±1.8），箭头放在 +X 端中间，尺寸适当大些保持可见。
        """
        # 机头（+X）端顶点位置；body 半长 L=0.6
        nose_x = 0.6
        base = nose_x + 0.05     # 箭头基面 x
        tip = base + 0.50        # 箭头尖端 x
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

    def set_body_model(self, mesh_data: MeshData, draw_edges: bool = False) -> None:
        """替换默认长方体为自定义 3D 模型（设备 STL/OBJ 用）。

        从 mesh_data 取顶点/面后保存，转交 _rebuild_device_body 按当前主题烘焙
        明暗。draw_edges 已弃用（大网格描边会糊成一团），保留签名兼容。
        """
        _ = draw_edges
        try:
            verts = np.asarray(mesh_data.vertexes(), dtype=np.float32)
            faces = np.asarray(mesh_data.faces())
        except Exception:
            return
        self._device_verts = verts
        self._device_faces = faces
        self._rebuild_device_body()

    def _body_shade_params(self):
        """返回 (base_rgb, ambient, light_dir)，按主题调亮度，保证白底/黑底都清晰。"""
        if self._theme == "light":
            # 白底：整体压暗，最亮面也只到中灰蓝，轮廓清楚不发白
            return (0.40, 0.46, 0.56), 0.42, (0.35, 0.45, 0.82)
        # 黑底：整体提亮，金属灰蓝
        return (0.66, 0.72, 0.82), 0.30, (0.35, 0.45, 0.82)

    def _bake_lambert_facecolors(self, verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
        """给每个三角面烘焙 Lambert 明暗（与背景无关），返回 (M,4) float32 RGBA。

        STL 无贴图/颜色，靠固定方向光的明暗体现立体细节。用 |n·L| 双面受光，
        避免 CAD 模型法线朝向不一致时背面变纯黑。intensity ∈ [ambient, 1]。
        """
        base, ambient, light = self._body_shade_params()
        tri = verts[faces.astype(np.intp)]                    # (M,3,3)
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)
        L = np.asarray(light, dtype=np.float64)
        L /= np.linalg.norm(L)
        lam = np.clip(np.abs(n @ L), 0.0, 1.0)                # (M,) 双面受光
        inten = ambient + (1.0 - ambient) * lam
        rgba = np.ones((faces.shape[0], 4), dtype=np.float32)
        rgba[:, :3] = (inten[:, None] * np.asarray(base)).astype(np.float32)
        return rgba

    def _rebuild_device_body(self) -> None:
        """用当前主题烘焙的 Lambert 面色重建设备 mesh（无 device 模型时 no-op）。"""
        if self._device_verts is None or self._device_faces is None:
            return
        rgba = self._bake_lambert_facecolors(self._device_verts, self._device_faces)
        md = MeshData(
            vertexes=self._device_verts, faces=self._device_faces, faceColors=rgba
        )
        prev_tf = None
        if self._body is not None:
            try:
                prev_tf = self._body.transform()
            except Exception:
                prev_tf = None
            self._gl_view.removeItem(self._body)
        # shader=None → 直接用烘焙好的面色，不被着色器二次调制（'shaded' 会把
        # 轮廓洗白，在白底上糊成黑块）。smooth=False 保持每面独立明暗。
        self._body = GLMeshItem(meshdata=md, smooth=False, drawEdges=False, shader=None)
        # 关键：update_attitude 旋转的是 self._aircraft，必须同步指向新 mesh
        self._aircraft = self._body
        self._gl_view.addItem(self._body)
        # 换上真实设备模型后，抽象红箭头既被大面板包住看不见、也不再需要
        self._nose_arrow.setVisible(False)
        # 沿用换模型前的姿态变换，避免重建时停在零姿态一帧
        try:
            self._aircraft.setTransform(prev_tf or self._nose_arrow.transform())
        except Exception:
            pass

    def try_load_device_model(self, hw_type: str) -> bool:
        """按用户覆盖、包内资源顺序加载设备 STL。

        找到并加载成功返回 True；否则保持默认占位长方体返回 False。
        用户可在 ~/.satellite_debug_tool/models/<hw_type>.stl 覆盖内置模型。
        """
        from satellite_debug_tool.ui.device_model_resources import (
            load_first_device_model,
            normalize_model_key,
        )

        model_key = normalize_model_key(hw_type)
        if not model_key:
            return False
        if getattr(self, "_loaded_model_hw", None) == model_key:
            return True   # 同一设备已加载，幂等

        from satellite_debug_tool.ui.stl_loader import load_stl, normalize_mesh

        def _load_mesh(model_path):
            verts, faces = load_stl(model_path)
            verts = normalize_mesh(
                verts, target_size=3.6,
                up_axis=self._model_up_axis,
                nose_axis=self._model_nose_axis,
                nose_sign=self._model_nose_sign,
                left_sign=self._model_left_sign,
            )
            return MeshData(vertexes=verts, faces=faces)

        loaded = load_first_device_model(model_key, _load_mesh)
        if loaded is None:
            return False
        _, mesh_data = loaded
        self.set_body_model(mesh_data, draw_edges=False)
        self._loaded_model_hw = model_key
        return True

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

        self.set_auto_bindings(
            roll=picks.get("roll", ""),
            pitch=picks.get("pitch", ""),
            yaw=picks.get("yaw", ""),
            ant_az=picks.get("ant_az", ""),
            ant_el=picks.get("ant_el", ""),
        )

    def set_auto_bindings(
        self,
        *,
        roll: str = "",
        pitch: str = "",
        yaw: str = "",
        ant_az: str = "",
        ant_el: str = "",
    ) -> None:
        """直接写入自动绑定结果；供 profile semantic role 优先覆盖。"""
        self._roll_ch = roll
        self._pitch_ch = pitch
        self._yaw_ch = yaw
        self._ant_az_ch = ant_az
        self._ant_el_ch = ant_el
        self._have_ant  = bool(self._ant_az_ch and self._ant_el_ch)
        if not self._have_ant:
            # 切设备或通道缺失时清掉残留扫描轨迹
            self._scan_trail.clear()
            self._trail_line.setData(pos=np.empty((0, 3), dtype=np.float32))

    def current_attitude_bindings(self) -> tuple[str, str, str]:
        """返回当前自动绑定的 (roll, pitch, yaw) DataStore key。"""
        return self.get_channel_selections()

    def current_pointing_bindings(self) -> tuple[str, str, str, str]:
        """返回当前自动绑定的 (tgt_az, tgt_el, ant_az, ant_el) DataStore key。"""
        return self.get_pointing_selections()

    def set_yaw_reference(self, reference: str) -> None:
        """标明 3D 当前 yaw 是绝对、相对、不可用，或来自旧固件。"""
        normalized = (
            reference
            if reference in {"legacy", "unavailable", "relative", "absolute"}
            else "unavailable"
        )
        if self._yaw_reference == normalized:
            return
        self._yaw_reference = normalized

        base = tr("Yaw:").rstrip(":：")
        if normalized == "legacy":
            self._yaw_name_lbl.setText(tr("Yaw:"))
            self._yaw_name_lbl.setToolTip("")
            return

        state_name = normalized.upper()
        self._yaw_name_lbl.setText(f"{base} [{state_name}]:")
        self._yaw_name_lbl.setToolTip(
            {
                "unavailable": "INTERNAL_INS_YAW_REFERENCE=UNAVAILABLE",
                "relative": "INTERNAL_INS_YAW_REFERENCE=RELATIVE; not north-referenced",
                "absolute": (
                    "INTERNAL_INS_YAW_REFERENCE=ABSOLUTE; "
                    "north-referenced, source may be inactive"
                ),
            }[normalized]
        )

    def yaw_reference(self) -> str:
        return self._yaw_reference

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

        # 保存 R_body2geo（FRD→NED），供波束反向解链使用
        self._body_rot = np.array(
            [[r00, r01, r02],
             [r10, r11, r12],
             [r20, r21, r22]], dtype=np.float64
        )
        # mesh 在 FLU 系内构造，要变换到 widget 世界系 NWU：M = P · R · P
        # 零姿态时 P·I·P = I，mesh 自然对齐世界轴（详见文件头说明）
        M = P_NED_NWU @ self._body_rot @ P_NED_NWU
        transform = np.eye(4, dtype=np.float32)
        transform[:3, :3] = M.astype(np.float32)
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
        self._set_default_camera()

    def _set_default_camera(self) -> None:
        """恢复兼顾设备与朝天波束轨迹的默认构图。"""
        self._gl_view.setCameraPosition(
            pos=_default_camera_center(),
            distance=_CAM_DISTANCE,
            elevation=_CAM_ELEVATION,
            azimuth=_CAM_AZIMUTH,
        )

    def update_pointing(
        self,
        tgt_az: Optional[float],
        tgt_el: Optional[float],
        ant_az: Optional[float],
        ant_el: Optional[float],
    ) -> None:
        """由 MainWindow 每帧调用。tgt_* 参数已弃用（恒为 None），仅保留签名兼容。

        ant_az/ant_el 经 `_ant_to_world_nwu` 反向解链得到 widget 世界系 (NWU)
        单位向量，乘半径 R_POINTING 画线。GEO 卫星跟踪锁定时，端点应稳定在
        固定空间点附近（与载体摇摆无关）。
        """
        _ = tgt_az, tgt_el  # 显式吃掉未使用参数
        empty2 = np.empty((0, 3), dtype=np.float32)

        if self._have_ant and ant_az is not None and ant_el is not None:
            self._ant_az_value = float(ant_az)
            self._ant_el_value = float(ant_el)
            v_world = _ant_to_world_nwu(ant_az, ant_el, self._body_rot) * R_POINTING
            self._ant_line.setData(
                pos=np.array([[0, 0, 0], v_world], dtype=np.float32)
            )
            self._scan_trail.append(v_world.copy())
            if len(self._scan_trail) >= 2:
                self._trail_line.setData(
                    pos=np.asarray(self._scan_trail, dtype=np.float32)
                )
            else:
                self._trail_line.setData(pos=empty2)
        else:
            self._ant_line.setData(pos=empty2)
            self._trail_line.setData(pos=empty2)

    # ---------- 主题 / 字号 (M6) ----------

    def _apply_label_styles(self) -> None:
        p = S.palette(self._theme)
        selector_px = S.font_px(11, self._scale)
        name_px = S.font_px(11 if self._readout_emphasis else 10, self._scale)
        value_px = S.font_px(15 if self._readout_emphasis else 10, self._scale)
        for lbl in self._value_name_labels:
            lbl.setStyleSheet(f"color: {p['text_muted']}; font-size: {name_px}px;")
        for val_lbl in (self._roll_val_lbl, self._pitch_val_lbl, self._yaw_val_lbl):
            val_lbl.setStyleSheet(
                f"color: {p['text']}; font-family: '{S.monospace_family()}'; "
                f"font-size: {value_px}px; font-weight: 600; min-width: 66px;"
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
        self._apply_scene_colors()
        self._apply_label_styles()

    def set_readout_emphasis(self, enabled: bool) -> None:
        """Enlarge attitude values for the customer overview only."""

        self._readout_emphasis = bool(enabled)
        self._apply_label_styles()

    def _apply_scene_colors(self) -> None:
        """按主题刷新 3D 场景里 grid / 波束 / 扫描轨迹 / 参考轴的颜色。

        浅色背景下原来的亮绿波束、淡蓝轨迹、深灰栅格几乎看不见，这里给浅色主题
        换成深而饱和的颜色，保证在白底上对比足够。"""
        light = self._theme == "light"
        if light:
            grid_color = (0.80, 0.80, 0.84, 0.45)   # 浅灰，淡淡的参考网格不抢眼
            beam_color = (0.00, 0.42, 0.00, 1.0)    # 纯饱和深绿，白底上细线也醒目
            trail_color = (0.05, 0.20, 0.75, 0.95)  # 饱和深蓝
            ax_x = (0.80, 0.00, 0.00, 1.0)          # 北：红
            ax_y = (0.00, 0.45, 0.00, 1.0)          # 西：深绿
            ax_z = (0.05, 0.10, 0.85, 1.0)          # 上：蓝
        else:
            grid_color = (0x3C / 255, 0x3C / 255, 0x3C / 255, 0.8)
            beam_color = (0.30, 0.95, 0.50, 1.0)
            trail_color = (0.50, 0.80, 1.0, 0.70)
            ax_x = (1.0, 0.0, 0.0, 1.0)
            ax_y = (0.0, 1.0, 0.0, 1.0)
            ax_z = (0.30, 0.45, 1.0, 1.0)
        if getattr(self, "_grid", None) is not None:
            self._grid.setColor(grid_color)
        self._ant_line.setData(color=beam_color)
        self._trail_line.setData(color=trail_color)
        axes = getattr(self, "_axes", {}) or {}
        for key, col in (("x", ax_x), ("y", ax_y), ("z", ax_z)):
            if key in axes:
                axes[key].setData(color=col)
        # 设备模型明暗按主题重新烘焙（白底压暗 / 黑底提亮）
        if getattr(self, "_device_verts", None) is not None:
            self._rebuild_device_body()

    def clear(self) -> None:
        """Reset the body to default orientation and clear pointing scene."""
        self._roll_value = 0.0
        self._pitch_value = 0.0
        self._yaw_value = 0.0
        self._aircraft.setTransform(np.eye(4, dtype=np.float32))
        self._nose_arrow.setTransform(np.eye(4, dtype=np.float32))
        self._body_rot = np.eye(3, dtype=np.float64)
        self._roll_val_lbl.setText("0.0°")
        self._pitch_val_lbl.setText("0.0°")
        self._yaw_val_lbl.setText("0.0°")
        self.set_yaw_reference("legacy")
        # 清指向矢量 / 扫描轨迹
        empty2 = np.empty((0, 3), dtype=np.float32)
        self._ant_line.setData(pos=empty2)
        self._trail_line.setData(pos=empty2)
        self._scan_trail.clear()
        self._ant_az_value = self._ant_el_value = 0.0
