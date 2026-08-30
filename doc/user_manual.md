# 卫星通信终端调试工具 — 用户手册

> **历史手册**：本文冻结于 M16 工程工作区，只描述当时的 Live / Playback / Log / Device 操作，
> 不覆盖 M18–M22 新增的客户工作台、试产工作区或 AFD01C 产品准入。当前架构以根目录
> `AGENTS.md` 为准；AFD01C 软件与真机边界见 `M22_AFD01C_upper_pc_adaptation_acceptance.md`。

**历史适用版本**：M16 源码构建（目标版本 `v1.1.0`，DEBUG protocol v2）

**工程 Debug profile 示例**：AFD01、AFD01C、ESA01、UFD45 及兼容 DEBUG v2 profile 的设备

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
- [9. 语言与主题](#9-语言与主题)
- [10. 常见问题](#10-常见问题)
- [11. 文件位置](#11-文件位置)

---

## 1. 快速开始

```bash
# 1. 建虚拟环境 + 安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r satellite_debug_tool/requirements.txt

# 2. 启动上位机
python3 -m satellite_debug_tool.main

```

看到状态条 `LINK OK` 绿灯 + Dashboard 卡片有数字 = 连接成功。

首次启动默认跟随操作系统语言：中文系统显示简体中文，其他系统显示 English。
可在“设置 → 语言”中随时切换，确认后立即生效，无需重启。

---

## 2. 界面总览

```
┌── 全局栏 (Satellite Debug Tool / 实时 / 回放 / Log / 设备 / 主题 / 更新 / 设置) ──┐
├── Live 工具栏 (Type / Remote / Connect / Debug / Record / Import / Clear) ─────┤
├── 状态条 (LINK / REC / BEAT / critical states...) ────────────────────────────┤
├── Dashboard (KPI 卡片 + 模式按钮) ──────────────────────────────────────────┤
│ ┌──────────┬────────────────┬──────────┬────────────────────┐
│ │ 通道面板 │ 分组/单图曲线  │ 3D 场景  │ 状态灯板/事件时间线 │
│ └──────────┴────────────────┴──────────┴────────────────────┘
├── 控制面板 (采样率 / 标记 / 通道使能 / 复位) ─────────────────────────────────┤
└── 状态栏 (链路类型 / FPS / Channels / Frames / Errors) ──────────────────────┘
```

Live 的主要显示区域由下位机 profile 驱动（通道 / 状态 / 事件 / 枚举）。新增设备型号通常无需改 UI；地图、3D 和模式控制这类高级语义目前仍依赖命名约定，后续会通过 profile 语义字段收口。

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
- 取消勾选会隐藏对应曲线；重新勾选后恢复显示
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

### 6.2 通道绑定

当前版本不再提供手动通道下拉选择，连接后会按 profile 通道名自动绑定：

- **姿态**：roll / pitch / yaw
- **指向**：ant_az / ant_el

若新型号通道命名不同，当前需要在 profile 命名上兼容，或后续通过 profile 语义字段扩展。

### 6.3 3D 场景元素

| 元素 | 颜色 | 触发条件 |
|------|------|---------|
| 机体 + 参考轴（X 红 / Y 绿 / Z 蓝） | — | 始终显示 |
| 天线实际法向 | 绿线 | ant_az + ant_el 通道都绑定 |
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

### Device Tab：参数管理

Device Tab 与 Live 共用当前连接。固件通过 profile capability 声明支持参数管理后：

- 连接握手完成后自动接收参数表；也可点“读取全部”手动刷新。
- 只读参数不显示可用的“应用”操作。
- 修改参数后点“应用”，上位机等待设备 ACK 和参数表读回值共同确认。
- 带警告标记的参数需要重启设备后业务模块才会采用新值。
- “恢复出厂”会调用设备端白名单恢复流程，执行前会二次确认。

设备参数键、枚举值和设备原始错误详情保持固件原文，不随界面语言翻译。

### Device Tab：OTA

固件声明 OTA capability 后可在 Device Tab 选择 app 镜像并上传：

1. 选择与目标硬件型号匹配的固件。
2. 根据现场需要决定是否暂停实时数据。
3. 点“上传并升级”，等待擦除、分块传输、校验和重启完成。
4. 设备重新上线后核对固件版本和关键参数。

上传期间不要关闭应用、断开网线或给设备断电。CRC、镜像完整性或硬件型号校验失败时，
上位机会保留设备返回详情；失败不等于设备已升级。

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

## 9. 语言与主题

### 语言

设置窗口提供：

| 选项 | 行为 |
|------|------|
| **跟随系统** | 中文地区统一使用简体中文，其他系统使用 English |
| **简体中文** | 固定使用 `zh_CN` |
| **English** | 固定使用 `en_US` |

点“确定”后所有第一方界面即时切换；点“取消”不会修改当前语言。切换语言不会重连设备、
清空数据、改变当前 Tab、通道勾选、曲线模式或 OTA 状态。

现场诊断可临时覆盖本次运行，环境变量不会写入配置：

```bash
SATELLITE_DEBUG_LOCALE=en_US python3 -m satellite_debug_tool.main
SATELLITE_DEBUG_LOCALE=zh_CN python3 -m satellite_debug_tool.main
```

设备上报内容、用户标记、自定义图表标题、协议字段、单位和工程数值保持原样。

全局栏右侧的主题按钮会循环切换：

### 主题

| 主题 | 背景 | 文字 | 用途 |
|------|------|------|------|
| **深色** | `#1E1E1E` | `#CCCCCC` | 桌面开发 |
| **深色·高对比** | `#000000` | `#F5F5F5` | 车载强光屏 500cd/m² |
| **浅色** | `#FAFAFA` | `#333333` | 日间外场 |

当前 UI 字号固定为 small（12px 基准），不再暴露字号切换入口。`styles.py` 内部仍保留 `FONT_SCALES` API 兼容旧测试和组件调用。

语言和主题配置持久化到 `~/.satellite_debug_tool/settings.json`：

```json
{
  "ui": {
    "language": "auto",
    "theme": "dark_hc",
    "active_tab_id": "live"
  }
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
A: 当前版本按 profile 通道名自动绑定 roll/pitch/yaw/ant_az/ant_el，不再保存手动绑定。新型号通道名不一致时，需要先让 profile 命名兼容，后续计划用 profile 语义字段解决。

### Q: 为什么没有字号下拉？
A: M7 后 UI 字号固化为 small，避免不同字号下工具栏和图表区域反复挤压。强光场景优先使用“深色高对比”主题。

### Q: 为什么切到 English 后仍能看到中文？
A: 第一方界面应全部显示英文；设备参数、状态名、事件、用户标记和导入日志属于原始业务数据，
会保持设备或用户提供的内容。如果按钮、标题或提示框仍有中文，请记录所在窗口和操作步骤。

### Q: 如何临时验证另一种语言而不改设置？
A: 启动前设置 `SATELLITE_DEBUG_LOCALE=en_US` 或 `zh_CN`。该值只影响当前进程。

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
├── settings.json          # UI 配置（主题 / 连接参数 / 默认路径 / 更新设置...）
├── profiles/
│   ├── afd01.json         # afd01 的 channel/state/event 表缓存
│   ├── esa01.json         # esa01 的 profile 缓存
│   └── ufd45.json         # 同上
└── (日志 / 录制文件默认在用户指定路径)
```

录制文件 `.sdb v2` 可放任意位置，回放时文件头带 profile，跨机器/跨设备可读。

---

## 附录 A：工具栏快捷按键

当前版本未实现全局快捷键（规划中）。常用操作：

- **Clear**：清曲线 / Dashboard / 事件 / 计数（保留 profile 与 StatePanel 状态）
- **Record**：切录制开关
- **Import**：兼容入口，打开离线 `.sdb`；常规回放建议使用“回放”Tab
- **Debug: ON/OFF**：下发 DEBUG_ENABLE，控制下位机是否上报

---

## 附录 B：开发者参考

- 当前工程约定与架构：[`AGENTS.md`](../AGENTS.md)
- 协议规范：[`doc/DEBUG设备协议接口规范_v2.md`](./DEBUG设备协议接口规范_v2.md)
- AFD01C 当前交付边界：[`doc/M22_AFD01C_upper_pc_adaptation_acceptance.md`](./M22_AFD01C_upper_pc_adaptation_acceptance.md)
- 历史路线基线：[`doc/upper_pc_function_definition_vnext.md`](./upper_pc_function_definition_vnext.md)
- 优化计划：[`doc/optimization_plan.md`](./optimization_plan.md)
- 开发日志：[`doc/development_log.md`](./development_log.md)
- 验收日志：[`doc/acceptance_log.md`](./acceptance_log.md)
- 英文手册：[`doc/user_manual_en.md`](./user_manual_en.md)
- 中英术语表：[`doc/i18n_terms.md`](./i18n_terms.md)

---

**反馈 / 问题**：请在开发日志对应里程碑下追加"遗留问题"条目，或直接联系开发。
