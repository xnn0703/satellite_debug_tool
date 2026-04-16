# 开发日志 — Protocol v2 + UI 重构

本文件为实施过程追踪文档，用于：
- 记录每个里程碑 (M1–M6) 的具体工作项与完成进度
- 记录与 `optimization_plan.md` / `DEBUG设备协议接口规范_v2.md` 的偏差（避免漏项或功能飘移）
- 汇总关键提交、测试结论、遗留问题

**参考文档**
- 协议规范：[doc/DEBUG设备协议接口规范_v2.md](./DEBUG设备协议接口规范_v2.md)
- 优化 plan：[doc/optimization_plan.md](./optimization_plan.md)

---

## 概览

| 项 | 值 |
|----|----|
| 启动日期 | 2026-04-16 |
| 上位机分支 | `feat/protocol_v2_refactor` (基于 `dev_3D_display`) |
| 下位机分支 | `feat/debug_protocol_v2` (基于 `feat/beampointing_refactor`) |
| 目标硬件 | afd01 (首发) + ufd45 (适配验证) |
| 当前阶段 | **M1 进行中** |

---

## 里程碑总览

| M | 主要内容 | 状态 | 对应验收 |
|---|---------|------|---------|
| M1 | 协议核心 + afd01 profile + 握手 + ProfileStore | 进行中 | §8.1.1 §8.4 §8.5 D-01~D-03 |
| M2 | State/Event 报文 + StatePanel/EventTimeline | 待开始 | §8.1.2 §8.3 |
| M3 | Dashboard + 分组曲线（完全 profile 驱动） | 待开始 | §8.1.3 §8.3 |
| M4 | 3D 场景 + 用户 mark 事件 | 待开始 | §8.1.5 |
| M5 | 异步录制 (.sdb v2) + ufd45 smoke test | 待开始 | §8.1.4 §8.5 D-04 |
| M6 | 主题/字体/布局微调 | 待开始 | §8.3 |

---

## M1 — 协议核心 + 握手 + ProfileStore

### 范围定义
- **协议核心层**（`shared/`）：设备无关，提供注册 API 和帧编解码
- **afd01 profile**（`target/afd01/`）：注册 afd01 全部 channel/state/event 并对接既有 topic
- **上位机协议层**：FrameReceiver v2 + 握手状态机
- **上位机元数据层**：ProfileStore + 本地缓存

### 下位机任务 (feat/debug_protocol_v2)

在 `code/shared/MiddleWare/components/debug/`：

- [ ] `debug_protocol.h/c`：帧结构升级到 v2，支持命令 0x01–0x0A
- [ ] `debug_registry.h/c`：提供 API
  - [ ] `debug_register_channel(name, unit, group_id, flags, *out_id)`
  - [ ] `debug_register_state(name, kind, enum_list, flags, *out_id)`
  - [ ] `debug_register_event(name, level, flags, *out_id)`
  - [ ] `debug_compute_table_ver()`（channel/state/event 注册完后计算）
- [ ] `debug_report.c`：上报接口
  - [ ] `debug_report_channel(id, value)` / batch
  - [ ] `debug_report_state(id, value)`（含变化触发）
  - [ ] `debug_report_event(id, level, payload)`
- [ ] `debug_session.c`：握手/心跳
  - [ ] 启动时发送 META_INFO + CHANNEL_DEFINE + STATE_DEFINE + EVENT_DEFINE
  - [ ] 周期（5s）重发 DEFINE 表
  - [ ] HEARTBEAT 1Hz（CPU load / free heap / tick）
  - [ ] 响应 REQUEST（按需重发指定表）
- [ ] CRC16：复用现有 `PublicLib/CRC`
- [ ] 接入 `USE_DEBUG` 编译开关

在 `code/target/afd01/application/app/debug_profile/`（新增目录）：

- [ ] `afd01_debug_profile.c`：
  - [ ] 注册 channel（trace SNR/信标、locate 姿态、modem 频点/功率、antenna 方向等）
  - [ ] 注册 state（trace_mode、ant_state、link_state、lock_flag、…）
  - [ ] 注册 event（ACQUIRE_DONE/LOST/CMD_ACK/INS_CALIB…）
  - [ ] 订阅既有 MCN topic 并在定时线程里打包上报
- [ ] `AppTaskCreate` 里创建 `debug_task`（优先级低于 trace/modem）

### 上位机任务 (feat/protocol_v2_refactor)

在 `satellite_debug_tool/core/protocol/`：

- [x] `frame_v2.py`：v2 常量 + 数据类
- [x] `codec_v2.py`：CRC 复用 + build_frame + build_control_* + 各 decode_*
- [x] `frame_receiver_v2.py`：字节流状态机，分发到 codec
- [x] `handshake.py`：QObject + Signal，`start()`/`tick(dt_ms)`/`feed(record)` API，
      自动发 4 条 REQUEST + 超时重发 + HEARTBEAT 心跳检测 + ready/link_lost/link_restored signal
- [x] **删除 v1 文件**：`frame_receiver.py` / `data_frame.py` / `helpers.py` + 4 个依赖旧测试

在 `satellite_debug_tool/core/profile/`（新增目录）：

- [x] `models.py`：`DeviceProfile` 聚合（channel/state/event 字典 + 三张 table_ver + meta）
- [x] `profile_store.py`：`ProfileStore(QObject)`，per-hw_type 分桶，`profile_changed` Signal，apply/get/export/import
- [x] `cache.py`：`ProfileCache`，JSON 落盘到 `~/.satellite_debug_tool/profiles/{hw_type}.json`（schema_version=1）

### DataStore / comm / UI 适配

- [x] `core/data/data_store.py`：按 `channel_id` 存储，内部 key `ch_{id:02d}`，写入 `DataReport`
- [x] `io/data_importer.py`：用 `FrameReceiverV2` 解析 .sdb，yield `DataReport`；csv 暂 stub（M5 重写）
- [x] `ui/main_window.py`：创建 `ProfileStore` + `Handshake`，连接成功后启动握手 + QTimer 100ms tick

### 测试（107 条全绿）

- [x] `tests/test_frame_v2.py`：数据类属性、flags 判定（12）
- [x] `tests/test_codec_v2.py`：build/decode 正反 + 错误样本（30）
- [x] `tests/test_frame_receiver_v2.py`：状态机完整闭环、CRC 错、分包、未知 cmd（12）
- [x] `tests/test_profile_store.py`：apply/signals/multi-hw/cache/export/import（16）
- [x] `tests/test_handshake.py`：start→ready、超时重发、heartbeat/link_lost/restored（10）
- [x] 旧 `tests/test_frame_receiver.py` / `test_data.py` / `test_io.py` / `test_integration.py` 删除
- [ ] 集成测试：回放 afd01 真实抓包（等下位机到位后在 M1 收尾阶段补）

### 验收对照（摘自 optimization_plan §8）
- [ ] F-01：连接后 500ms 内收到 META + 三张 DEFINE（需下位机）
- [ ] F-02：100Hz DATA_REPORT 连续 10 分钟不丢帧，CRC 错 = 0（需下位机）
- [ ] C-03：非 0x02 protocol_ver 断连并提示（需下位机/模拟器）
- [ ] D-01：afd01 接入完整显示（需下位机）
- [ ] D-02：profile 缓存命中无需重新握手（单元已覆盖，待端到端）
- [ ] D-03：连接未知 hw_type，UI 无崩溃，显示 `ch_{id:02d}`（单元已覆盖，待端到端）

---

## M2 — State/Event（待开始）

（M1 完成并通过验收后细化）

---

## M3 — Dashboard + 分组曲线（待开始）

---

## M4 — 3D + 用户 mark（待开始）

---

## M5 — 异步录制 + ufd45 smoke（待开始）

---

## M6 — 主题字体（待开始）

---

## 偏差记录

> 与 `optimization_plan.md` / `DEBUG设备协议接口规范_v2.md` 的实际差异（发现后即时记录，含原因与补偿措施）。

### 2026-04-16 — 放弃 v1 兼容层
- **决策**：v2 完全取代 v1，不在上位机维护"检测并回退 v1 帧格式"的逻辑，也不在下位机维护 `USE_DEBUG_PROTO_V2` 开关的 v1 路径
- **原因**：本项目上下位机同仓同步发布，保留兼容层只增加代码路径、测试负担和误解风险
- **影响**：
  - 删除上位机 `core/protocol/frame_receiver.py` / `data_frame.py` / `helpers.py` 与相关 v1 测试
  - 重写 `core/protocol/__init__.py` 只导出 v2 接口
  - 下位机新协议核心只实现 v2；`trace.c` 现有 `debug_report_data()` 调用点重写为 v2 API
  - 旧 `.sdb` v1 文件不再由主工具读取；如需要留一个独立 `sdb_v1_convert.py` 一次性脚本（非 M1 范围）
- **对应文档更新**：
  - `optimization_plan.md` v1.2：§5.1 / §5.6 / §6.1 / §7 M5 / §8.1 / §8.4 / §9 风险表
  - `DEBUG设备协议接口规范_v2.md`：§1 兼容性、§5.3 v1 兼容段（删）、§11 整节改写

---

## 变更日志

| 日期 | 分支 | 摘要 |
|------|------|------|
| 2026-04-16 | 上位机 `dev_3D_display` | commit `aef6483` — docs: 协议 v2 规范 + 优化 plan |
| 2026-04-16 | 上位机/下位机 | 各自创建开发分支，启动 M1 |
| 2026-04-16 | 上位机 `feat/protocol_v2_refactor` | 新增 `frame_v2.py` / `codec_v2.py` / `frame_receiver_v2.py` 三件套 |
| 2026-04-16 | 文档 | plan v1.2 + 协议规范更新：**废除 v1 兼容层** |
| 2026-04-16 | 上位机 `feat/protocol_v2_refactor` | 删除 v1 代码 + DataStore/importer 切 v2；MainWindow 协议入口切换 |
| 2026-04-16 | 上位机 `feat/protocol_v2_refactor` | 新增 `core/profile/`（models + ProfileStore + cache） |
| 2026-04-16 | 上位机 `feat/protocol_v2_refactor` | 新增 `core/protocol/handshake.py`；MainWindow 自动握手 + 100ms tick |
| 2026-04-16 | 上位机 `feat/protocol_v2_refactor` | 上位机 M1 任务完工，测试 107/107 通过 |

---

## 遗留问题 / 待决策

*暂无*
