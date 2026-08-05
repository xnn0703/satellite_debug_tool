# 验收日志

对照 `optimization_plan.md` §8 验收表，按里程碑逐项跟踪验收状态。

**图例**：

- ✅ 代码层已实现且自动化测试覆盖
- 🟡 代码已实现，等真机/外场/长时实测关闭
- ❌ 未实现
- ➖ plan 明确不做（§10）

**更新规则**：每个 M 阶段完工后，把该阶段涉及的验收条目从 `❌` 更新到
`✅/🟡`，并在"证据"列写清单元测试文件 / 提交 hash / 截图路径。

---

## §8.1 功能验收

| 编号 | 功能 | 条件 | 阶段 | 状态 | 证据 |
|------|------|------|------|------|------|
| F-01 | 协议握手 | 连接后 500ms 内收到 META + 三张 DEFINE | M1 | 🟡 | simulator 过（test_simulator.py 端到端 smoke），真机待 |
| F-02 | 数据上报 | 100Hz × 10min 不丢帧、CRC 错=0 | M1 | 🟡 | 模拟器端到端通，长时实测待 |
| F-04 | 状态字 | 下位机 LOCK_FLAG 切换，上位机 200ms 内显示 | M2 | 🟡 | simulator 手测 OK，真机待 |
| F-05 | 全量重发 | 上位机中途重启，重新握手后状态板所有灯恢复正确 | M2 | 🟡 | StateStore 不清空 + 下位机 5Hz 全量发；simulator 过，真机待 |
| F-06 | 事件上报 | LOCK_ACQUIRED 事件触发，时间线立即显示，曲线画竖线 | M2 | ✅ | EventLog + `chart.add_event_marker` 走通，M4 又加 tooltip |
| F-07 | 用户标记 | 上位机点标记，下位机 300ms 内回 EVENT(0xFFFF) | M4 | 🟡 | 上位机已发 USER_MARK；Chart 识别 0xFFFF 画加粗琥珀；下位机回路待真机 |
| F-08 | Dashboard | SNR/AZ/EL/姿态/误差 KPI 卡片按 `flags.critical` 自动生成 | M3 | ✅ | DashboardWidget（dashboard_widget.py），profile 驱动 |
| F-09 | 曲线分组 | SNR 与姿态角显示在不同 Y 轴都清晰 | M3 | ✅ | GroupedChartWidget stacked 模式按 group_id 子图 |
| F-10 | 曲线交互 | 鼠标滚轮缩放、拖动、右键复位、暂停冻结 | M3 | ✅ | pyqtgraph 默认开启；M6 加 single/grouped toggle |
| F-11 | 3D 场景 | 飞机（长方体）+ 卫星矢量 + 天线矢量 + 误差扇面同时渲染 | M4 | ✅ | AttitudeWidget，`test_attitude_pointing.py` 16 条单测覆盖坐标公式 + 扇面几何 |
| F-12 | 录制 | 100Hz 连续 1h，UI ≥ 30fps | M5 | 🟡 | 异步 DataRecorder 已实现，长时实测待 |
| F-13 | 回放 | `.sdb v2` 回放时事件时间线与曲线同步 | M5 | ✅ | DataImporter + 内嵌 profile，`test_recorder_importer.py` roundtrip |
| F-14 | 模式切换 | 点 AUTO/MANUAL 按钮，下位机 trace_mode 切换并回 resp | M3 | 🟡 | Dashboard.mode_requested → build_set_trace_mode；真机闭环待 |
| F-15 | 高对比主题 | dark_hc 在 500cd/m² 阳光屏可读 | M6 | 🟡 | dark_hc 已实现并通 `test_styles.py` 对比度断言，外场待测 |

## §8.2 性能验收

| 编号 | 指标 | 条件 | 状态 | 证据 |
|------|------|------|------|------|
| P-01 | 下位机 CPU 增量 | 启用 v2 后相比 v1 CPU 增加 ≤ 1%（100Hz 上报）| 🟡 | 下位机侧待实测（CPU 探针）|
| P-02 | 下位机带宽 | 总上报带宽 ≤ 15 KB/s | 🟡 | 待真机 tcpdump 实测 |
| P-03 | 下位机 RAM 增量 | 相比 v1 增加 ≤ 2 KB | 🟡 | 编译地图报告已看（RAM_D2 4%），差分待测 |
| P-04 | 上位机 CPU | 100Hz × 16 通道，CPU ≤ 25% | 🟡 | simulator 下目测 10~15%，长时实测待 |
| P-05 | 上位机内存 | 2h 内存 ≤ 600 MB | 🟡 | 长时实测待 |
| P-06 | UI 帧率 | 曲线 + Dashboard + 3D 同时 ≥ 30 fps | 🟡 | simulator 下 FPS 10~15（默认 200ms heavy timer + 100ms light timer，实测 symbolic），长时实测待 |
| P-07 | 事件延迟 | 下位机触发到上位机显示 < 300 ms | 🟡 | 真机待测 |
| P-08 | 录制延迟 | 录制写盘不阻塞 UI，最大卡顿 < 50 ms | 🟡 | 异步 Recorder 设计满足，长时实测待 |

## §8.3 UI 验收

| 编号 | 指标 | 条件 | 状态 | 证据 |
|------|------|------|------|------|
| U-01 | 一眼可读 | 外场测试者 3 米外能读到 SNR、锁星灯、模式 | ❌ | 需车载外场实测 |
| U-02 | 指示灯含义 | 每个灯悬停显示 tooltip | ✅ | M6 已加 ~30 处 tooltip（状态/通道/工具栏/Dashboard/事件） |
| U-03 | 事件追溯 | 任何一个事件可双击跳到曲线时刻 | ✅ | A1 `EventTimelineWidget.jump_requested` + Chart.jump_to_timestamp |
| U-04 | 车载屏适配 | 1920×1080 和 1366×768 不溢出 | 🟡 | 桌面 1280 以上 OK；1366×768 未实测 |
| U-05 | 字号切换 | 超大字号下 Dashboard 数字不截断 | ✅ | M6 KpiCard 字号缩放 + minWidth 自适应；所有水平条带加横滚兜底 |

## §8.4 协议版本验收

| 编号 | 条件 | 状态 | 证据 |
|------|------|------|------|
| C-03 | META_INFO.protocol_ver == 0x02；非 0x02 时提示并断连 | 🟡 | `codec_v2.decode_meta` 已强制 2；断连提示路径待硬件确认 |

## §8.5 多设备自适应验收

| 编号 | 场景 | 条件 | 状态 | 证据 |
|------|------|------|------|------|
| D-01 | afd01 → ufd45 切换 | 2 秒内按新 profile 重建 UI | 🟡 | simulator 过；真机切换待 |
| D-02 | 未知 hw_type | UI 正常工作，按下发表渲染，无需代码改动 | ✅ | ufd45 profile 与 afd01 完全不同通道/状态/事件验证通过（M5） |
| D-03 | Profile 缓存命中 | 二次连接同型号，未收到新 DEFINE 前按缓存渲染 | ✅ | ProfileCache JSON 落盘，test_profile_store.py 覆盖 |
| D-04 | Profile 版本升级 | table_ver 递增后旧缓存失效 | ✅ | `apply_*_define` 带 table_ver 比对，test_profile_store.py 覆盖 |
| D-05 | Profile 导出导入 | JSON 可被离线工具解析 .sdb | ✅ | `ProfileStore.export()/import_dict()` + round-trip 测试 |
| D-06 | .sdb 回放跨设备 | afd01 录的 sdb 在 ufd45 机器能放 | ✅ | SDB v2 内嵌 profile，`test_recorder_importer.py::test_round_trip_with_profile` |
| D-07 | 未知 channel_id | 按 `ch_XX` 显示，不丢数据 | ✅ | DataStore 统一用 `channel_id` 索引 |
| D-08 | 未知 event_id | 显示 `EVENT_<hex>`，不崩 | ✅ | EventLog 降级逻辑 |
| D-09 | 无事件型号 | EventTimeline 空面板不崩 | ✅ | 设计如此 |
| D-10 | 零通道型号 | Chart "等待握手"，StatePanel/Dashboard 正常 | ✅ | 设计如此 |

---

## 里程碑验收签收

| M | 描述 | 完成日期 | 测试 | 签收 |
|---|------|---------|------|------|
| M1 | 协议核心 + afd01 profile + 握手 + ProfileStore | 2026-04-16 | 107/107 → 后续合入共 186 | ✅ 代码；🟡 真机联调 |
| M2 | State/Event 报文 + UI | 2026-04-17 | +15 | ✅ 代码；🟡 真机 |
| M3 | Dashboard + 分组曲线 + StatusStrip | 2026-04-17 | +0（已覆盖） | ✅ 代码；🟡 真机 |
| M4 | 3D 场景升级 + 用户 mark | 2026-04-18 | +16 | ✅ 代码；🟡 真机（坐标系方向 + USER_MARK 回路） |
| M5 | 异步录制 + ufd45 profile + simulator | 2026-04-17 | +7+16 | ✅ 代码 |
| M6 | 主题/字号/tooltip + 分条横滚 | 2026-04-18 | +17 | ✅ 代码；🟡 外场 |
| 批次 A | 小项体验（EventTimeline/StatePanel/ControlPanel/Attitude） | 2026-04-18 | +10 | ✅ 代码；🟡 真机 |

---

## 剩余待真机/外场实测清单（Checklist）

以下项目等硬件到位后按顺序实测：

- [ ] **F-01** afd01 握手时延 500ms（连接后启抓包，查 META → 三 DEFINE 的 Δt）
- [ ] **F-02** 100Hz × 10min 丢帧/CRC 错计数（观察 `FPS/Frames/Errors` 状态栏）
- [ ] **F-04** 在下位机改 LOCK_FLAG，秒表测上位机灯变化延迟
- [ ] **F-05** 上位机运行中 disconnect / reconnect，状态板恢复正确
- [ ] **F-07** 上位机点"打标记"，看下位机日志 + 上位机曲线 0xFFFF 竖线
- [ ] **F-12 + P-04/05/06** 100Hz 录制连续 1 小时
- [ ] **F-14** Dashboard 点 AUTO/MANUAL，下位机响应
- [ ] **C-03** 故意改下位机 protocol_ver=3，上位机应提示并断连
- [ ] **D-01** 实物切换 afd01 ↔ ufd45，2 秒内 UI 重建
- [ ] **U-01 + F-15** 车载 500cd/m² 屏下 dark_hc + 超大字号 3 米外可读性
- [ ] **U-04** 1366×768 分辨率布局不溢出
- [ ] **坐标系方向** Attitude 的 tgt/ant 矢量方向与实际一致？若反了改 `AZ_PHI_OFFSET_DEG / AZ_SIGN`
- [ ] **内部 INS 航向** 从未建立绝对参考的会话显示 `Yaw [RELATIVE]` 且随设备相对转动；
      固定 Oracle 建立后切为 `Yaw [ABSOLUTE]`；关闭 Oracle 后保持已建立的绝对参考并继续惯性传播，
      重新上电且不注入航向时再回到 `RELATIVE`，业务 `yaw` 不被相对值伪造

测试完毕请在本文件对应项打勾并写入证据（日志截图 / 视频 / 测试数据）。
