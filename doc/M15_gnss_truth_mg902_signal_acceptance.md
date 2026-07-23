# M15 GNSS 状态真值与 MG902 卫星信号可视化验收标准

> 日期：2026-07-22
> 状态：上位机软件验证完成；真机验收待完成
> 计划：`doc/M15_gnss_truth_mg902_signal_plan.md`

## A. GPS_FIX 和 profile

- [x] MG902 3D 的 `STATE_REPORT(value=2)` 在顶部状态条和 Dashboard 都显示 `3D`。
- [x] Bynav DGNSS/RTK_FLOAT/RTK_FIXED 分别显示正确标签。
- [x] 设备侧 `STALE` 与上位机链路侧 `UNKNOWN` 有不同文本/样式。
- [x] 两个 widget 只消费同一 StateStore，不存在第二套 fix 推导逻辑。
- [x] 相同 `table_ver`、不同枚举内容的 STATE_DEFINE 能刷新 profile 和两个 widget。
- [x] 相同 `table_ver`、完整内容相同不会造成重复 UI 重建。
- [x] 相同状态值的周期报告刷新 last received，但不篡改 last changed。

## B. Live/Playback 生命周期

- [x] Debug OFF、串口/UDP 断线、切换设备后旧 GPS_FIX 立即清除。
- [x] 超过 3 个状态上报周期无新 STATE_REPORT 后显示 UNKNOWN/链路 stale。
- [x] 重新连接同一 `hw_type` 后使用设备新下发的完整枚举。
- [x] Playback 打开第二个文件、关闭文件和清除时不残留第一个文件状态。
- [x] Playback 暂停超过 3 秒不会按墙钟把已记录状态改成 stale。
- [x] 旧 SDB 按内嵌旧 profile 回放；不会无依据地重解释历史 RTK。

## C. 协议兼容

- [x] `0x0D/0x0E` 既有 Bynav golden payload 解码结果不变。
- [x] `0x0F/0x10` 与固件 C codec 的 golden payload、分片边界和 CRC 一致。
- [x] 未安装新 decoder 的旧 receiver 将 `0x0F/0x10` 返回 RawFrame，decode error 不增长。
- [x] 新 receiver 对版本、长度、count、chunk index/count、total 和重复冲突分片严格校验。
- [x] SDB 版本不变，录制内容仍为原始帧和原始时间顺序。

## D. GnssStore 和 UI

- [x] Legacy Bynav 报告进入 source=BYNAV/namespace=UG016。
- [x] MG902 报告进入 source=MG902/namespace=UBX_M9。
- [x] 相同 raw signal ID 在不同 source 下映射为各自正确的信号名称。
- [x] 未知 signal ID 显示 raw fallback，不误标成已知频段。
- [x] Bynav→MG902、MG902→Bynav 切源后旧 Sky/Signal/pending/history 不残留。
- [x] Sky/Signal 分别计算 age/stale，一侧更新不能刷新另一侧。
- [x] 只有 NAV-SAT 时显示天空图但不显示虚假逐频点柱。
- [x] NAV-SIG 明细保留 source/system/SV/raw signal/freq/CN0/quality/correction/flags。
- [x] MG902 `quality_ind=4..7` 单独显示 `LOCK`；C/N0 为 0 时仍保留锁定状态，但不计入有效 CNR
  或柱图。
- [x] MG902 已锁定且 C/N0 大于 0 时，即使 `prUsed/crUsed/doUsed` 均为 0，仍计入有效 CNR 并显示柱图。
- [x] 明细 `Lock / Used` 分别显示质量锁定与参与导航解算，不再将两者混为同一状态。
- [x] NAV-SAT 只绘制 `0<=elevation<=90`、`0<=azimuth<=360` 的记录；越界记录保留在记录数中，
  但不投影到天空图。
- [x] 卫星记录数与可绘星数分别统计；有卫星记录但没有有效方位时显示明确空态。
- [x] 有 NAV-SIG 但没有 quality lock 时显示“暂无锁定信号”；已有 lock 但 C/N0 无效时显示
  “已有锁定信号，但暂无有效 C/N0”，两者都不显示“等待数据”。
- [x] MG902 Playback 恢复 q4/q7 未 USED 信号柱图、无效天空坐标、Lock/Used 明细和统计。
- [x] Live 与 Playback 使用同一渲染组件并产生一致快照。
- [x] GNSS 窗口文案不再把所有来源都写成 GSV/RANGECMPB。
- [x] 顶部星座筛选和着色开关使用 HeightForWidth FlowLayout；在 1024、800、520 逻辑像素宽度下，
  每个复选框宽度均不小于自身 `sizeHint()`，同行控件互不相交。
- [x] 宽度不足时筛选项优先换行且宿主高度同步增加；source badge 与统计摘要位于独立信息行，
  两区不重叠，统计摘要允许换行。
- [x] 顶部统计字体只按 12、11、10 px 三档兜底，任何宽度下不低于 10 px，窗口重新拉宽后恢复
  12 px。
- [x] Live 与 Playback 共用响应式顶部；Playback 快照控制行的显示、滑块和前后按钮不受影响。
- [x] 响应式测试应用真实 light/small QSS，并在结束后恢复 QApplication 原样式，避免测试间污染。
- [x] macOS Cocoa 下 QSS 应用和 scale 切换后，复选框 geometry 仍覆盖最终 `sizeHint()` 且互不重叠；
  开发机完整 GNSS 专项通过。测试代码不强制 Cocoa，普通 CI 保持 offscreen 可运行。
- [x] “天空图按 C/N₀ 着色”开启时，由天空图画布在左上角直接 overlay 竖向连续色带；明确标出顶部强端
  `>=51 dB-Hz`、中间刻度 `40/30 dB-Hz`、底部弱端 `<=20 dB-Hz`，并用灰色样例说明“无 C/N₀ 数据”。
- [x] 图例颜色与天空图 `_cn0_color()` 共用同一 `20..51 dB-Hz` 映射，范围外颜色按端点钳位，
  不允许只用近似文案描述另一套离散阈值。
- [x] 图例不是 layout 子控件；开启和关闭着色前后，天空图 widget geometry、size、圆心和半径完全相同。
- [x] Overlay 卡片位于画布内且处于天空外圆加 12 px 卫星标记保护区之外，不覆盖 N/E/S/W、网格和
  `elev=0/az=315°` 的左上边缘卫星；在 light/dark、small/medium 和 1024/800/520 逻辑像素宽度下
  无裁切、无重叠，Live/Playback 共用相同显示逻辑。

## E. 自动化与真机边界

- [x] 新增 profile、state lifecycle、codec、store、mapping、Live/Playback 测试全部通过。
- [x] `PYTHONPATH=. pytest satellite_debug_tool/tests` 全绿。
- [x] `git diff --check` 通过。
- [ ] MG902 Live 与 u-center 逐星/逐信号对照一致。
- [ ] Bynav Live 与厂商工具逐星/逐信号对照一致。
- [ ] 两类设备各录制一份 SDB，并在 Playback 恢复相同 source、卫星、信号、C/N0 和时间顺序。
- [ ] 真机断线/停帧时两处状态同时进入正确的 STALE/UNKNOWN，不保留绿色旧状态。

## 通过规则

- 自动化测试通过只标记“上位机软件验证完成”。
- MG902、Bynav、SDB 和链路状态真机项全部通过后，才标记 M15 功能验收完成。
