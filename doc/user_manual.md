# 卫星通信终端调试工具 — 用户手册

**适用版本**：`satellite_debug_tool` v2 (protocol v2)  
**适用设备**：afd01 (Ka 频段)、ufd45 (Ku 频段) 及未来同协议型号

---

## 目录

- [1. 快速开始](#1-快速开始)
- [2. 界面总览](#2-界面总览)
- [3. 连接下位机](#3-连接下位机)
- [4. 数据查看](#4-数据查看)
- [5. 事件与状态](#5-事件与状态)
- [6. 3D 姿态与指向](#6-3d-姿态与指向)
- [7. 控制下位机](#7-控制下位机)
- [8. 录制与回放](#8-录制与回放)
- [9. 主题与字号](#9-主题与字号)
- [10. 常见问题](#10-常见问题)
- [11. 文件位置](#11-文件位置)

---

## 1. 快速开始

```bash
# 1. 建虚拟环境 + 安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 启动上位机
python -m satellite_debug_tool

# 3. (可选) 无真机时可以用 UDP 模拟器
python tools/device_simulator.py --profile afd01 -v
# 上位机选 UDP / 127.0.0.1 / 4004 → Connect
```

看到状态条 `LINK OK` 绿灯 + Dashboard 卡片有数字 = 连接成功。

---

## 2. 界面总览

```
┌── 工具栏 (Type / Remote / Connect / Debug / Record / Import / Clear / 主题 / 字号) ──┐
├── 状态条 (LINK / REC / BEAT / critical states...) ────────────────────────────┤
├── Dashboard (KPI 卡片 + 模式按钮) ──────────────────────────────────────────┤
│ ┌────────────────────┬──────────┬────────────────┐
│ │  分组 / 单图曲线   │  3D 场景 │  状态灯板       │
│ │                    │          │  事件时间线     │
│ └────────────────────┴──────────┴────────────────┘
├── 控制面板 (采样率 / 标记 / 通道使能 / 复位) ─────────────────────────────────┤
├── 通道选择 (按 profile 展示，可勾选) ─────────────────────────────────────────┤
└── 状态栏 (链路类型 / FPS / Channels / Frames / Errors) ──────────────────────┘
```

所有区域都由下位机 profile 驱动（通道 / 状态 / 事件 / 枚举），
新增设备型号时上位机**零改动**。

---

## 3. 连接下位机

### 串口

- `Type: Serial` → 选端口 + 波特率（默认 115200）→ `Connect`
- 连接成功后工具栏出现 `Disconnect`，`Debug: OFF/ON` 可切换

### UDP

- `Type: UDP` → 填 Remote IP / Remote Port / Local Port
  - 默认 `192.168.1.12 : 4004`（下位机 WizNet）
  - Local Port 是本机绑定端口（默认 45678）
- 点 `Connect`；2 秒内应完成协议 v2 握手
  （META_INFO + CHANNEL_DEFINE + STATE_DEFINE + EVENT_DEFINE）

### 握手成功的标志

- StatusStrip 的 **LINK** 变绿 `LINK OK`
- Dashboard 出现 KPI 卡片和模式按钮
- StatePanel 出现按子系统分组的状态行
- Chart 左下有曲线开始滚动
- 状态栏 `Channels: N / Frames: 持续增长`

### 若握手超时

- 检查下位机 `USE_DEBUG` 是否编译进去，且 debug 任务已启动
- 检查网线 / 防火墙 / 端口冲突（`lsof -i :45678` 看有没有其它进程占用）
- 上位机状态栏会提示 `Heartbeat timeout (link lost)`

---

## 4. 数据查看

### 4.1 Dashboard（KPI 卡片）

- 显示 `flags.critical == 1` 的通道（设备决定显示哪些）
- 大号等宽数字 + 单位 + 通道名
- 超出 `[display_min, display_max]` 范围时**卡片背景变红**
- 数字字体锁定等宽族（JetBrains Mono / Consolas / SF Mono），不随字号档位换字型

### 4.2 分组曲线 / 单图曲线

顶部有 `单图 / 分组` 切换：

- **单图**：所有通道叠在一张大图，快速扫视整体趋势
- **分组**：按 profile 的 `group_id` 纵向分子图，适合多量纲对比（量纲差大的 SNR 和姿态角分开看）

X 轴显示相对启动时间（秒），防止 pyqtgraph 自动切 ks 单位抖动。
默认保留最近 **120 秒**，ChannelBuffer 容量 **30000 点（100Hz × 5min）**。

**交互**：

- 鼠标滚轮：缩放
- 左键拖动：平移
- 右键：重置视图 / 保存图片
- 顶部 toggle：切单图 / 分组模式

### 4.3 通道选择面板

底部一排带色圆点的复选框：

- 颜色与曲线颜色对应
- 取消勾选可在通道标签区隐藏（曲线本身仍在，为了对比保留）
- 当前值实时显示在右侧

---

## 5. 事件与状态

### 5.1 状态灯板（StatePanel）

按子系统**可折叠**分组（M6 批次 A4）：

- 跟踪（Trace） / 调制解调（Modem） / 射频（RF） / 导航（INS）/ 其它
- 点击分组标题 `▼` 折叠 / 展开
- 标题后 `(N)` 显示本组状态数

每行：

- **BOOL**：圆点 + ON/OFF 文本。`flags.inverse` 时颜色反转
- **ENUM**：圆点 + 当前枚举名，颜色取 `enum_item.level`
  （INFO 绿 / WARN 黄 / ERROR 红 / NEUTRAL 灰）

**最近变化项在 2 秒内外框闪琥珀高亮**（A3），方便看出刚刚切换的是哪个。

### 5.2 事件时间线（EventTimeline）

最新在顶部的倒序事件列表：

```
HH:MM:SS.mmm  [LVL] hw_type  event_name  · payload 预览
```

**过滤**：

- 级别下拉：All / INFO+ / WARN+ / ERROR
- 关键字：按事件名模糊（不区分大小写）
- 清空：清本地缓冲（不影响下位机继续上报）

**跳转曲线（A1/A2）**：

- **双击**任一事件行 → 曲线 X 视窗跳到那个时刻
- **右键** → "在曲线上定位"

**用户标记**（ControlPanel "打标记" 按钮下发后，下位机回 `EVENT(0xFFFF)`）：

- 事件行有 ⚑ 前缀
- Chart 画**加粗琥珀实线**的标记竖线

---

## 6. 3D 姿态与指向

### 6.1 机体模型

默认是**扁长方体**（相控阵卫通终端外形，长边沿 +X 机头方向）。
顶面机头端有 8% 削角，用来视觉辨别朝向。

未来具体设备的 STL/OBJ 模型接入后，这里会自动换成设备实物造型。

### 6.2 通道绑定（2 行 7 路）

**姿态**（第 1 行）：roll / pitch / yaw

**指向**（第 2 行）：tgt_az / tgt_el / ant_az / ant_el

握手时优先**按 hw_type 分桶**恢复你之前的绑定（A6）；若该 hw 没保存过，
会按 profile 通道名自动匹配（`roll`/`pitch`/`yaw`/`tgt_*`/`ant_*`）。

手动改下拉后会自动保存到 `~/.satellite_debug_tool/settings.json`
的 `attitude.<hw_type>.<axis>_channel` 键。

### 6.3 3D 场景元素

| 元素 | 颜色 | 触发条件 |
|------|------|---------|
| 机体 + 参考轴（X 红 / Y 绿 / Z 蓝） | — | 始终显示 |
| 卫星矢量 | 红线 | tgt_az + tgt_el 通道都绑定 |
| 天线实际法向 | 绿线 | ant_az + ant_el 通道都绑定 |
| 误差扇面 | 半透明琥珀 | 两矢量都存在 |
| 扫描轨迹（最近 300 点） | 淡蓝点迹 | ant 通道绑定 |

**视角控制**：

- 鼠标拖动：旋转
- 滚轮：缩放
- 点"**复位视角**"按钮恢复默认（distance=10, elev=30, azim=45）

### 6.4 坐标系约定

```
+X 机头  +Y 左翼  +Z 天顶
az = 0° 指机头右（-Y），从 +Z 俯视逆时针增大
el = 0° 天顶（+Z），90° 水平面
```

若发现实际方向与显示相反，修改 `satellite_debug_tool/ui/attitude_widget.py`
顶部常数 `AZ_PHI_OFFSET_DEG / AZ_SIGN` 即可。

---

## 7. 控制下位机

ControlPanel（Dashboard 下方那一行）：

| 控件 | 作用 | 协议帧 |
|------|------|-------|
| 采样率下拉 | 改下位机 DATA_REPORT 频率 | `CONTROL.SET_SAMPLE_RATE` |
| 标记文本 + "⚑ 打标记" | 发送用户标记，曲线画琥珀竖线 | `CONTROL.USER_MARK` |
| **通道使能…** | 打开对话框勾选要上报的通道 | `CONTROL.CHANNEL_ENABLE_MASK` |
| 复位统计 | 清下位机内部计数/累计值 | `CONTROL.RESET_STATS` |

Dashboard 下方的**模式按钮组**（AUTO / MANUAL / STANDBY 等）：

- 由 profile 里 `flags.critical == 1` 的 ENUM 状态字生成
- 点击发送 `SET_TRACE_MODE` 子命令
- 按钮组内容随 profile 变（ufd45 可能是另一套枚举）

### 通道使能对话框（A5）

- 网格列出 profile 里所有通道
- **全选 / 全不选 / 反选**快捷键
- 勾选确定 → 下发 64bit bitmask → 下位机按 mask 采样

---

## 8. 录制与回放

### 录制

- 点工具栏 `Record` → 弹文件对话框选保存路径
- 按钮变红、StatusStrip 的 REC 闪红灯
- 使用 **异步后台线程**写盘，UI 不卡顿
- 文件格式 `.sdb v2`：文件头内嵌当前 hw_type 的 profile 快照

### 回放

- 点工具栏 `Import` → 选 `.sdb` 文件
- **回放前自动按文件内嵌 profile 重建 UI**（跨机器、跨设备型号都能放）
- 打开时 DataStore 会清空，避免与实时数据混

**注意**：v1 格式 `.sdb` 不再兼容，如需要可用独立的 `sdb_v1_convert.py`（未随本仓发布）。

---

## 9. 主题与字号

工具栏最右两个下拉：

### 主题（3 档）

| 主题 | 背景 | 文字 | 用途 |
|------|------|------|------|
| **深色** | `#1E1E1E` | `#CCCCCC` | 桌面开发 |
| **深色·高对比** | `#000000` | `#F5F5F5` | 车载强光屏 500cd/m² |
| **浅色** | `#FAFAFA` | `#333333` | 日间外场 |

### 字号（4 档）

| 档 | 基准比例 | 基准 12px → |
|---|---------|-------------|
| 小 | 1.00 | 12px |
| 中 | 1.17 | 14px（默认） |
| 大 | 1.50 | 18px |
| 超大 | 1.83 | 22px |

- **Dashboard KPI 数字字体锁等宽族**，字号随档缩放
- **所有水平密集行**（工具栏 / StatusStrip / Dashboard / 通道选择）
  在超大档下**出现横向滚动条**，不会被挤出屏幕
- **工具栏字号固定 12px**（不随全局字号缩放），保证任意档下都能找到主题/字号下拉

配置持久化到 `~/.satellite_debug_tool/settings.json`：

```json
{
  "ui.theme": "dark_hc",
  "ui.font_scale": "xlarge"
}
```

---

## 10. 常见问题

### Q: 连上后数字一直是 `—`？
A: 只完成握手但没发 `DEBUG_ENABLE`。点工具栏 `Debug: OFF` 切到 `ON`。

### Q: 事件时间线空着？
A: 该 profile 可能没注册事件（比如某个调试阶段的固件），属正常。
也可能 EventLog 被清空过，Clear 按钮不清 profile 但清事件缓冲。

### Q: 切了设备型号后 3D 绑定变了？
A: A6 已按 hw_type 分桶保存绑定，切回原 hw 应该恢复。如果是**首次**连接
另一个 hw 型号，会走 auto_bind 规则自动匹配。

### Q: 超大字号下找不到字号下拉？
A: 工具栏已锁定 12px 不随字号缩放，且装不下时底部会出现横滚条，
往右滚就能看到。

### Q: 曲线每隔几秒整体闪一下？
A: 已于 2026-04-18 根因修复（`ProfileStore.apply_meta` 幂等化 +
UI 签名兜底）。若仍遇到请升到最新版。

### Q: 录制文件很大？
A: 100Hz × 16 通道大约 15 KB/s → 1 小时 ~55 MB。正常。
停录后可用 `gzip file.sdb`，回放前解压即可。

### Q: 怎么看当前 profile 里有哪些通道？
A: 两个地方：
- Dashboard + Channel Selection 展示所有通道
- `~/.satellite_debug_tool/profiles/<hw_type>.json` 是持久化的完整 profile

---

## 11. 文件位置

用户配置 / 缓存目录：`~/.satellite_debug_tool/`

```
~/.satellite_debug_tool/
├── settings.json          # UI 配置（主题 / 字号 / attitude 绑定 / 连接参数...）
├── profiles/
│   ├── afd01.json         # afd01 的 channel/state/event 表缓存
│   └── ufd45.json         # 同上
└── (日志 / 录制文件默认在用户指定路径)
```

录制文件 `.sdb v2` 可放任意位置，回放时文件头带 profile，跨机器/跨设备可读。

---

## 附录 A：工具栏快捷按键

当前版本未实现全局快捷键（规划中）。常用操作：

- **Clear**：清曲线 / Dashboard / 事件 / 计数（保留 profile 与 StatePanel 状态）
- **Record**：切录制开关
- **Import**：打开离线 `.sdb`
- **Debug: ON/OFF**：下发 DEBUG_ENABLE，控制下位机是否上报

---

## 附录 B：开发者参考

- 协议规范：[`doc/DEBUG设备协议接口规范_v2.md`](./DEBUG设备协议接口规范_v2.md)
- 优化计划：[`doc/optimization_plan.md`](./optimization_plan.md)
- 开发日志：[`doc/development_log.md`](./development_log.md)
- 验收日志：[`doc/acceptance_log.md`](./acceptance_log.md)
- 下位机模拟器：`tools/device_simulator.py`（纯 UDP，可 `--profile afd01|ufd45`）

---

**反馈 / 问题**：请在开发日志对应里程碑下追加"遗留问题"条目，或直接联系开发。
