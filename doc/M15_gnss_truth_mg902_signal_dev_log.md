# M15 GNSS 状态真值与 MG902 信号可视化开发记录

> 日期：2026-07-22
> 状态：上位机软件验证完成；真机验收待完成

## 基线

- Bynav 天空图与逐信号 C/N0 基线提交：`5224e58`。
- 基线专项测试：29 passed。
- 基线全量测试：549 passed、4 warnings。
- 真机 Bynav 与厂商工具、Live 录制到 Playback 对照仍待完成。

## 实施记录

- 2026-07-22：确定保留顶部状态条和 Dashboard，两处共用同一 StateStore。
- 2026-07-22：确定新增 `0x0F/0x10`，旧 `0x0D/0x0E` 保持兼容。
- 2026-07-22：确定区分设备侧 NO_FIX/STALE 与上位机链路侧 UNKNOWN。
- 2026-07-22：`StateSnapshot` 分离 `last_received_wallclock` 与 `last_changed_wallclock`；相同值周期帧只刷新
  received。Live 3.5 秒收不到 STATE_REPORT 时显示 UNKNOWN，Debug OFF、断线和切换设备立即清状态；
  Playback 打开、关闭、换文件时清状态，暂停不使用墙钟判 stale。
- 2026-07-22：`ProfileStore` 仅在 `table_ver` 和规范化完整内容均相同时跳过更新；保留旧 SDB 的内嵌旧
  profile，不重解释历史值 3 的 `RTK`。State/Channel/Event 三类 DEFINE 均使用相同判等原则。
- 2026-07-22：新增 `0x0F GNSS_SAT_REPORT`、`0x10 GNSS_SIGNAL_REPORT` 解码和 source-aware
  `GnssStore`。MG902 使用 `UBX_M9` namespace，Bynav legacy 使用 `UG016`；Sky/Signal 独立维护
  age、stale、pending，完整报告切源时清除上一源快照和历史。decoder 严格校验 64 条/片、92 条总上限
  和 canonical 分片形状；重复片的 records 或 flags 冲突都会丢弃整轮。
- 2026-07-22：GNSS Live/Playback 共用同一 Store/widget；只有 NAV-SAT 时只显示天空图，NAV-SIG 明细
  保留 receiver-native signal ID、freq、quality、correction 和 raw flags；UNKNOWN source 使用 RAW fallback。
- 2026-07-23：根据 MG902 真机画面修正信号过滤语义。NAV-SIG `quality_ind=4..7` 单独定义
  `LOCK`，锁定且 C/N0 大于 0 才进入柱图和有效 CNR；`prUsed/crUsed/doUsed` 仅作为独立的
  `USED` 诊断。明细列分别显示 `LOCK` 和 `USED`，因此 q7/CN0=0 仍显示锁定但不生成零高柱。
- 2026-07-23：NAV-SAT 只投影仰角 `0..90`、方位角 `0..360` 的记录；窗口分别显示卫星记录数和
  可绘星数。收到记录但方位/仰角均无效时显示明确说明；收到 NAV-SIG 但没有锁定信号时显示
  “暂无锁定信号”；已有 lock 但 C/N0 无效时显示“已有锁定信号，但暂无有效 C/N0”，均不再误报
  为尚未收到数据。新增 MG902 Playback widget 回归，覆盖 q4/q7 未 USED 柱图和无效天空坐标。
- 2026-07-23：根据 Retina 1024 逻辑像素宽度真机窗口修正 GNSS 顶部排版。星座筛选和着色开关
  移入 HeightForWidth FlowLayout，控件始终按完整 `sizeHint()` 排布，空间不足时增加行高换行；
  source badge 和统计摘要拆到独立信息行，摘要允许换行。信息行字号按实际完整文本宽度在
  12/11/10 px 间降档且拉宽后恢复，筛选项不降档；传入非 small scale 时使用对应缩放字号。
  Live/Playback 共用同一实现，Playback 快照控制行保持独立。
- 2026-07-23：macOS Cocoa 复核发现，QSS polish 前后 `QWidgetItem` 可能保留原生 layout-item
  margin/旧尺寸，导致 offscreen 通过但 Cocoa 中 widget geometry 小于最终 `sizeHint()`。GNSS
  QCheckBox 在加入 FlowLayout 前设置 `WA_LayoutUsesWidgetRect`，并在字号变化后失效布局缓存；
  不使用显式 Fixed policy，也不修改共享 FlowLayout，避免影响 StatusStrip 等既有消费者。Cocoa
  下 1024/800/520、small/medium scale 和 Playback 快照行均已回归。
- 2026-07-23：为“天空图按 C/N₀ 着色”增加画布内左上角 overlay 色带。色带与卫星标记共用
  `_cn0_color()` 的 `20..51 dB-Hz` 映射，顶部显示 `>=51 强`、中间显示 `40/30`、底部显示
  `<=20 弱`，标题旁的灰色块表示无数据。图例由 `SkyPlotWidget.paintEvent()` 直接绘制，不是
  layout 子控件；关闭着色只停止绘制，天空图 geometry、圆心和半径不变。字号按真实字体度量
  从大到小选择，卡片保持在天空外圆加 12 px 的卫星标记保护区之外；360×360 最小画布和
  `elev=0/az=315°` 左上边缘卫星也不会相交。
  macOS Cocoa 下绘制渐变前显式清除半透明卡片 brush，避免原生后端把卡片 alpha 乘入色带。
  同步修正 Bynav tooltip：RANGECMPB 最大值覆盖 GSV SNR 参与着色时，提示同时显示实际着色值和
  卫星记录值，不再出现颜色与提示数值不一致。

## 软件验证结果

- 完整测试：`PYTHONPATH=. pytest satellite_debug_tool/tests`，625 passed、4 条既有
  `datetime.utcnow()` 弃用告警。
- MG902 GNSS 专项：`PYTHONPATH=. pytest satellite_debug_tool/tests/test_gnss_protocol_store.py`，
  83 passed；包含截图中的 GPS q7/28、q7/29、q1/0、q4/25 和 BDS q7/36 组合、q7/CN0=0
  的 LOCK 保留、MG902 Playback、无效天空坐标空态，以及真实 QSS 下 1024/800/520 逻辑像素
  响应式排版、窄屏换行/字号兜底/宽屏恢复、C/N₀ 色带和 Playback 快照行回归。
- macOS 原生验证：`QT_QPA_PLATFORM=cocoa ... test_gnss_protocol_store.py`，83 passed；普通
  offscreen 专项同为 83 passed，测试本身不强制平台。已目视检查 1024/520 逻辑像素的 light/small
  overlay 色带、刻度、无数据样例与卫星标记，并单独检查 360×360 最小天空图；开关前后天空圆
  没有平移或缩放。
- `python3 -m py_compile` 已检查 GNSS widget 与专项测试文件；`git diff --check` 通过。
- 定向回归覆盖同版本 profile 刷新、双处 GPS_FIX 一致、Live/Playback 生命周期、跨源 namespace、92 条
  分片、旧 SDB 内嵌 profile，以及旧 receiver 将 `0x0F/0x10` 作为 RawFrame 且 decode error 不增长。
- 固件 C 与 Python codec golden bytes、CRC 和分片边界一致；`git diff --check` 通过。

## 待真机验证

- MG902 与 u-center 逐星/逐信号对照，并录制真实 SDB 后验证 Playback。
- Bynav 与厂商工具逐星/逐信号对照，并录制真实 SDB 后验证 Playback。
- 真机 Debug OFF、断线、GNSS 停帧、恢复和切源时，核对顶部状态条与 Dashboard 同步进入
  NO_FIX/STALE/UNKNOWN 及恢复。
