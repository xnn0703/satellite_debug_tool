# M15 Plan：GNSS 状态真值与 MG902 卫星信号可视化

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-07-22 |
| 状态 | 软件实施完成；真机验收待完成 |
| 固件主计划 | `3-satlite_comm_terminal/code/shared/MiddleWare/DALib/DAL/plan_gnss_fix_truth_mg902_signal.md` |
| 验收文档 | `doc/M15_gnss_truth_mg902_signal_acceptance.md` |
| 开发记录 | 实施时新增 `doc/M15_gnss_truth_mg902_signal_dev_log.md` |

## 1. 背景和已确认根因

截图中的顶部 `GPS_FIX: RTK` 和 Dashboard `GPS_FIX / RTK` 不是两个独立状态。二者的数据链都是：

```text
设备 STATE_REPORT(state_id=2, value=3)
→ FrameReceiverV2
→ 同一个 StateStore
├─ StatusStripWidget：GPS_FIX: RTK
└─ Dashboard EnumStatusChip：RTK
```

上位机目前只是按设备下发的 STATE_DEFINE 把数值 3 翻译为 `RTK`，不会检查卫星数、坐标、
MG902 `gnssFixOK` 或 Bynav `pos_type`。设备把普通 3D 解错误写成值 3 后，两处 UI 会忠实地一起显示
错误的 RTK。

修复原则是：接收机协议判据和跨源归一必须在固件完成；上位机负责一致展示、状态新鲜度、缓存正确性和
原始诊断数据查看，不在两个 widget 中各写一套 MG902/Bynav 业务逻辑。

## 2. 目标

1. 顶部状态条与 Dashboard 展示同一份、由固件归一后的 GNSS fix class。
2. 区分设备侧 `NO_FIX`、设备侧 `STALE` 和上位机链路侧 `UNKNOWN`。
3. 防止同进程重连、Debug OFF、断线和 Playback 换文件后继续显示旧 RTK。
4. 在现有 GNSS Live/Playback 窗口同时支持 Bynav GSV/RANGECMPB 与 MG902 NAV-SAT/NAV-SIG。
5. 保留 source、receiver-native signal ID 和原始质量标志，便于与 u-center/厂商工具核对。
6. 保持旧 profile、旧 Bynav SDB 和未知新命令的向前兼容行为。

## 3. GPS_FIX 展示契约

### 3.1 固件公共枚举

上位机不重新映射原始接收机值，只显示 profile 中的公共状态：

| wire value | label | UI 建议 |
|---:|---|---|
| 0 | `NO_FIX` | 警告色；设备有新数据但无位置解 |
| 1 | `2D` | 警告色 |
| 2 | `3D` | 正常色 |
| 3 | `RTK_FIXED` | 正常色；保留旧值 3 的 ABI |
| 4 | `DGNSS` | 正常色 |
| 5 | `RTK_FLOAT` | 正常/提示色 |
| 6 | `STALE` | 错误或灰色；设备 3 秒无新 PVT |

`GNSS_SOURCE` 为单独的非 critical 状态，至少支持 `UNKNOWN/BYNAV/MG902/MANUAL`。状态条不因来源
不同而更改 fix 判断，GNSS 详情窗口用它显示来源并处理切源。

### 3.2 Live 状态生命周期

| 场景 | 行为 |
|------|------|
| 周期收到相同 STATE_REPORT | 数值不变，但刷新 `last_received_wallclock`，不修改 `last_changed_wallclock` |
| 设备 GNSS 超时但 debug 在线 | 显示设备上报的 `STALE` |
| Debug OFF | 清空 Live `StateStore`，显示 UNKNOWN/空态 |
| 串口/UDP 断线 | 立即清空状态，不保留绿色 RTK/3D |
| 超过 3 个 1 Hz 全量状态周期未收到报告 | 显示链路侧 UNKNOWN/STALE UI |
| 重连同一 `hw_type` | 重新应用收到的完整 DEFINE 内容 |

`StateSnapshot` 增加最后接收时间和最后变化时间两个概念；相同值的周期帧只刷新前者。状态 widget
从 store 读取 freshness，不各自维护计时器。

### 3.3 Playback 状态生命周期

- 打开新文件、关闭文件和清除时必须先清 `StateStore`。
- 状态由回放游标前最近一条记录决定；暂停回放不能因为墙钟经过 3 秒而变 stale。
- SDB 内嵌的旧 profile 继续决定旧数据的枚举文本。
- 不自动把历史 `RTK` 改成 `3D`。旧固件没有记录 source/raw flags 时无法可靠区分真实 RTK 和旧 bug；
  详情处标记为 legacy/ambiguous 即可。

## 4. Profile 刷新修正

当前 `ProfileStore.apply_state_define()` 在 `table_ver` 相同且已有 states 时直接跳过。固件的 table version
目前更接近“注册条目数”，只修改枚举内容时可能不变化。

修改为：

```text
table_ver 相同 AND 规范化后的完整 StateDefineTable 内容相同 → 跳过
否则 → 更新内存 profile、重建索引并通知 UI
```

比较内容包括 state ID/name/type/flags 以及每个 enum 的 value/level/name。Channel/Event define 同类逻辑
一并审计，但本阶段至少完成 StateDefine 的回归测试。磁盘 cache 将来启用预热时也复用同一内容比较逻辑。

## 5. MG902 GNSS 报告兼容方案

### 5.1 保留旧 Bynav 协议

已存在的协议保持字节不变：

- `0x0D GNSS_SKY_REPORT v1`：Bynav GSV，解码为 source=`BYNAV`、namespace=`UG016`；
- `0x0E GNSS_CNR_REPORT v1`：Bynav RANGECMPB，解码为 source=`BYNAV`、namespace=`UG016`。

旧 SDB 继续按上述路径回放，不升级 SDB 文件版本。

### 5.2 新增 MG902 source-aware 报告

新增两个顶层命令，旧 receiver 会作为 RawFrame 安全忽略：

| Cmd | 名称 | 数据来源 |
|---:|---|---|
| `0x0F` | `GNSS_SAT_REPORT` | UBX-NAV-SAT |
| `0x10` | `GNSS_SIGNAL_REPORT` | UBX-NAV-SIG |

两个 payload 都带显式 `version`、`source`、`timestamp_ms`、`report_id`、分片元数据和 flags。wire source
固定为 `UNKNOWN=0/MG902=1/BYNAV=2/MANUAL=3/MS6222=4`，后续只能追加，不能重排。每片最多
64 条，完整快照最多 92 条。

逐卫星记录至少保留：

```text
system, sv_id, cno_dbhz, elevation_deg, azimuth_deg, raw_sat_flags
```

逐信号记录至少保留：

```text
system, sv_id, raw_signal_id, freq_id, cno_dbhz,
quality_ind, corr_source, iono_model, pr_res, raw_sig_flags
```

星座 wire enum 沿用现有 `GPS/GLONASS/SBAS/Galileo/BDS/QZSS/NavIC/Other`。`raw_signal_id` 不在固件
伪装成 Bynav 类型；上位机按 `(source, system, raw_signal_id)` 查频段和名称。

### 5.3 统一内部模型

扩展 `GnssStore` 的内部记录：

- `source` 和 `signal_namespace`；
- `raw_signal_id` 与规范化的 band/name；
- receiver-specific raw quality/flags；
- MG902 `quality_ind=4..7` 单独表示 `LOCK`；在此基础上 `cn0_dbhz>0` 才表示 C/N0 可用于柱图和
  有效值统计；`raw_sig_flags` 的 `prUsed/crUsed/doUsed` 只表示是否参与当前解算，三者不得合并；
- Sky、Signal 各自独立的 `last_update_wallclock`、age 和 stale；
- pending 分片也属于特定 source，切源时整体丢弃。

当收到与当前不同 source 的完整报告，先清空上一源 Sky/Signal/pending/history 再写入新源；不能让旧
Bynav 柱图和新 MG902 天空图混在同一快照中。

## 6. GNSS 窗口行为

- 标题/空态从 Bynav 专用的 `GSV/RANGECMPB` 改为来源无关的“天空图/逐信号 C/N0”。
- 显示 source badge（Bynav、MG902、Legacy/Unknown）。
- 天空图可使用 NAV-SAT 的卫星级 C/N0 着色，但该值只代表卫星视图。
- 开启“天空图按 C/N₀ 着色”时，由 `SkyPlotWidget` 在天空图画布左上角直接 overlay 一块紧凑的
  竖向连续色带：`>=51 dB-Hz` 绿色强端在上、`<=20 dB-Hz` 红色弱端在下，灰色代表没有
  C/N₀ 数据。图例不是 layout 子控件，不得占用 splitter 宽度；关闭着色时只停止绘制 overlay。
- C/N₀ overlay 根据真实字体度量选择紧凑字号，并放在天空外圆及卫星标记保护区之外；不能覆盖
  N/E/S/W、网格或地平线卫星。开启/关闭前后天空图 widget geometry、圆心和半径必须完全相同。
- NAV-SAT 仅在 `0<=elevation<=90` 且 `0<=azimuth<=360` 时绘制；统计分别显示接收的卫星记录数和
  实际可绘星数，不能把未知方位记录计作已绘制卫星。
- 逐频点柱和明细只有收到 RANGECMPB 或 NAV-SIG 时才显示，不从天空图复制数据。
- MG902 柱图和“有效 CNR”要求 `quality_ind=4..7` 且 C/N0 大于 0，不要求信号已参与导航解算；
  明细中的 `LOCK` 只由 quality 判断，`USED` 只由 raw flags 判断。
- 空态区分三种情况：未收到 NAV-SIG、已收到但没有 quality lock、已有 lock 但没有正值 C/N0。
- Signal 映射按 source namespace 分发；未知 signal ID 显示 `Signal <raw id>`，不能猜名称。
- 明细表保留原始 system/SV/signal/freq/quality/flags，供厂商工具逐项对照。
- Sky stale 不影响 Signal age，Signal stale 也不能被新的 Sky 帧“刷新”。
- Live/Playback 继续共用同一个 widget 和 store 模型。
- 顶部信息区拆为两层：第一层仅放星座筛选和天空图着色开关，使用基于真实 `sizeHint()` 的
  FlowLayout；第二层单独放 source badge 和统计摘要，避免长统计文本反向压缩复选框。
- 星座复选框始终使用完整 `sizeHint()`，宽度不足时优先换行，不能裁切 indicator 或文字。FlowLayout 宿主必须
  正确声明 HeightForWidth，让换行后增加的高度参与父布局计算。
- 统计摘要允许换行；字体仅在完整显示仍受限时按 `12→11→10 px` 降档，不能低于 10 px，窗口恢复
  足够宽度后必须恢复 12 px。字体适配按 Qt 逻辑像素和实际文本宽度计算，不能依赖 Retina 物理像素。
- Live/Playback 复用同一响应式顶部实现；Playback 快照控制行保持独立，不参与筛选区换行。

## 7. 实施顺序

### M15-A：状态真值展示

- [x] 扩展 GPS_FIX enum 和 GNSS_SOURCE 元数据测试夹具。
- [x] 修复 StateDefine 同版本内容刷新。
- [x] StateStore 记录 last received/last changed。
- [x] Live 断线、Debug OFF、切设备时清状态。
- [x] Playback 换文件/清空时清状态，并按游标恢复状态。
- [x] 两处 widget 共用 freshness 结果和 UNKNOWN 样式。

### M15-B：MG902 协议与 Store

- [x] 增加 `0x0F/0x10` dataclass、codec、receiver 分发和 exports。
- [x] 保留 `0x0D/0x0E` legacy decoder 和旧 SDB 测试。
- [x] Store 增加 source/namespace、独立 age 和切源清理。
- [x] 增加 UBX_M9 constellation/signal 映射，保留 unknown fallback。

### M15-C：Live/Playback UI

- [x] GNSS widget 来源中立化并增加 source badge。
- [x] 明细表显示 MG902 raw quality/flags。
- [x] MG902 `LOCK`、有效 C/N0 与 `USED` 分离，柱图只使用 lock 且 C/N0 大于 0 的记录。
- [x] NAV-SAT 无效天空坐标不投影，卫星记录数和可绘星数分别显示。
- [x] 已收信号但尚未锁定与未收到信号使用不同空态。
- [x] Live 接入新报告并录制原始帧。
- [x] Playback 重建新报告，同时保持旧文件行为。
- [x] 顶部筛选区使用 HeightForWidth FlowLayout，复选框不受 source/统计文本挤压。
- [x] source/统计区独立排版，统计文本支持 12/11/10 px 兜底与换行、宽屏字号恢复。
- [x] 真实 QSS 下覆盖 1024/800/520 逻辑像素宽度，并验证 Live/Playback 共用行为。
- [x] macOS Cocoa 原生平台完成响应式与完整 GNSS 专项验证；普通 CI 继续使用 offscreen，不依赖显示环境。
- [x] 天空图 C/N₀ 着色开启时在画布左上角 overlay 竖向连续色带、20/30/40/51+ 刻度和灰色无数据
  标识，关闭时只停止绘制；
  图例与 `_cn0_color()` 使用同一范围和端点色。
- [x] C/N₀ overlay 在 1024/800/520 逻辑像素宽度下不改变天空圆几何、不覆盖保护区，
  Live/Playback 行为一致。

### M15-D：验证和文档

- [x] 运行新增专项测试及全量 pytest。
- [x] 使用固件 golden bytes 做跨语言 codec 对照。
- [x] 更新 DEBUG v2 协议规范、开发记录和用户说明。
- [ ] 完成 MG902/Bynav 真机 Live/Playback 对照，记录仍待验收项。

## 8. 建议提交边界

1. `fix(profile): 同版本定义按内容刷新`
2. `fix(ui): 清理 GNSS 旧状态并显示 freshness`
3. `feat(protocol): 增加 MG902 卫星信号报告`
4. `feat(gnss): 支持 MG902 天空图和逐信号 C/N0`
5. `docs(gnss): 更新状态与卫星信号协议`

每个提交说明分别列出“已验证”和“待真机验证”；旧 Bynav GNSS 窗口的未提交改动必须先确认基线，
不能在提交时混入无关文件。

## 9. 风险与回退

- 新命令不改变旧命令；MG902 窗口异常时可以忽略 `0x0F/0x10`，不影响 DATA/STATE/OTA。
- Profile 内容比较需要规范化，避免仅因对象顺序不同导致 UI 重建抖动。
- Playback 的 freshness 使用录制时间轴，Live 使用 monotonic wall clock，二者不能复用同一判定函数参数。
- 旧 SDB 的错误 RTK 无法离线自动纠正，只能保持原样并标注来源不足。
