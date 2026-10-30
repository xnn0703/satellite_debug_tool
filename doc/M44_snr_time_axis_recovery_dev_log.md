# M44 SNR 时间轴问题调查记录

## 2026-10-28：定位与复现

### 源码调用链

- 当前读数：`ServiceFastState -> ProductServiceStore._fast -> snapshot.operation.snr_db -> _snr_readout`。
- Product 曲线：`ServiceFastState -> _snr_timestamp.add -> _snr_history -> snr_history -> _snr_curve/Y range`。
- Debug 曲线：`DataReport -> ChannelBuffer（保留混合启动样本） -> unwrap_u32_series -> channel_history -> curve`。
- 温度历史：`ServiceComponentHealth -> _component_temperature_timestamp.add -> per-component history`。
- 连接：`begin_connection -> product_store.clear -> unwrapper.reset`；同连接 records 的低 uptime 心跳/同身份不会经过该连接初始化路径。
- Runtime：`core.feed_bytes/apply_records` 在 `_after_records` 之前执行；presence epoch 恢复只处理身份授权，不改变历史时间锚点。

### 确定性探针结果

| 场景 | 最新数值 | 曲线/窗口实际结果 |
|---|---|---|
| 正常时间前进，0 -> 18.5 dB | 18.5 | Y 范围正常包含 18.5（控制实验通过） |
| Product，600 s -> 重启 1 s | 18.5 | 曲线仍为 600 s / 0，Y 为 `[-2,2]` |
| 旧历史全 NaN，Y 预置 `[-1,1]`，随后新段 18.5 | 18.5 | 旧 NaN 继续保留，Y 原样不动 |
| 重启后只调用 clear_history，再到 4 s / 19 dB | 19 | 历史为空，旧锚点继续拒收 |
| Product store 全量 clear 后到 5 s / 19.5 dB | 19.5 | 曲线恢复，Y `[17.5,21.5]` |
| Debug v2 混合 600 s / 0 与 1 s / 18.5 | 18.5 | 曲线只剩旧 600 s / 0 |
| 温度 600 s / 30 °C -> 新段 1 s / 50 °C | 最新帧 50 | converter 历史仍为 30 |

独立 Core 探针加入同一身份重新广播、Heartbeat 600000 -> 1000、Product Fast 和 Debug DataReport：connection generation 仍为 1，读数 18.5，Product/Debug 两条历史均停留在 600 s / 0。此实验无实际设备、无网络发送。

诊断文件：

- `/tmp/sdt-m44-snr-probe.json`：UI 读数、曲线与范围逐阶段结果。NaN 是探针直接构造的测试数据；并非本轮采集的现场报文。
- `/tmp/sdt-m44-core-reboot-probe.json`：Core 同连接心跳与身份重报实验。
- `/tmp/test_m44_snr_diagnosis.py`、`/tmp/sdt-m44-diagnosis-tests.log`：以目标行为断言，当前 **5 failed, 3 passed in 0.85s**。失败为 Product、Debug、新段显示清空、NaN/Y 范围、模块温度；控制为正常 Y 更新、u32 回绕和单个旧包拒收。
- 原项目测试不引入这些诊断失败文件；实施阶段将相应用例加入正式回归，并通过会话级证据完整验证新段确认合同。

原项目相关基线：客户总览、Product Service、DeviceSessionCore 与 endpoint Runtime 共 **142 passed in 1.20s**（`/tmp/sdt-m44-existing-tests.log`）。这说明当前既有测试覆盖尚未发现本轮新复现的同连接时间段恢复缺口。

### 最小复现

```python
from satellite_debug_tool.core.product import ProductServiceStore
from satellite_debug_tool.core.protocol import ServiceFastState

store = ProductServiceStore()
for timestamp, snr in [(600000, 0.0), (1000, 18.5)]:
    store.feed(ServiceFastState(
        1, timestamp, 1 << 11, 0, 0, False, 0, 0, False,
        0.0, 0.0, 0.0, 0.0, 0.0, snr,
    ))
print(store.snapshot().operation.snr_db.value)  # 18.5
print(store.snr_history())                     # 旧 600 s / 0
```

### 覆盖缺口与修复约束

现有测试覆盖显式 `store.clear()` 后重新接收低时间样本；缺少同连接设备时间线重启的整链路覆盖。自然 u32 回绕与旧包拒收本身是正确合同，修复应增加明确采集代际，避免将普通反向时间直接解释为物理重启。

本轮完成调查、复现和计划落盘；运行逻辑保持当前状态。M43 Shell 未提交改动保持原样；打包由后续明确指令触发。现场原始记录尚未提供，因此已证明源码故障链能够复现现象，现场每次发生的触发时刻与实际 NaN/Inf 报文仍需记录对照。


## 2026-10-28：确认后的实现与回归

用户回复“可以”后实施，工作区原有 M43 改动保留。本轮源码修复如下：

1. 增加 `core/session/telemetry_clock.py`。Core 先准入，再更新唯一 Store，随后通知 UI/业务观察者。反向候选期间最新值与历史共同保留原事实，确认后一次重置 Product 动态事实、Debug、状态、GNSS、Orbit 及历史时间锚点，再回放已确认的新段。
2. `ProductServiceStore.begin_telemetry_epoch()` 是动态事实重建入口，`clear()` 复用它并额外清理静态事实。`clear_history()` 继续只清显示。Product 的两个历史 unwrapper 保留其展开职责；启动识别统一在 Core。
3. 连接 generation 与传输 capability 保持；操作 scope 新增 telemetry epoch。参数与 Product 控制监听新代际，OTA/安装标定重启阶段复用既有身份/版本/回读验证。Runtime 在数据准入确认时撤销旧身份授权，后续新身份广播重建授权。
4. `feed_bytes()` / `apply_records()` 返回实际准入记录；Runtime 的 presence/身份/观察者只消费这些记录。原始 datagram 信号仍在解析前触发，原始 SDB 留存迟到/候选包。
5. 总览曲线选择实际遥测来源；Product 已有遥测时显示清空保持空曲线，只有身份广播的现有 Debug 历史入口持续可用。空历史恢复默认范围，有限历史驱动 Y 自适应；`connect="finite"` 明确保留无效点断线。
6. SDB loader 复用时间准入模型并建立新段显示偏移。SQLite 派生缓存插入 NaN 边界，夹具时间相关器显式隔离代际。原始录制保持完整，既有 u32 近邻映射提取为公共工具并移除原私有重复实现。
7. 补充大于 u32 半区 uptime 后重启的主机时间证据；补充移动围栏随采集推进到 51 天/自然回绕的验收。没有增加重试、轮询、设备特例或 UI 启动探测。

### 回归证据

- 同一正式回归核心场景在 `/tmp/sdt-m44-baseline` 的 HEAD 隔离快照运行：**12 failed, 7 deselected in 1.03s**，日志 `/tmp/m44-baseline-failures.log`。快照只增加测试及其纯模型导入依赖，运行逻辑使用原 HEAD。
- 新采集代际专项：**21 passed in 0.90s**，`/tmp/m44-new-final.log`。
- 会话/Runtime/客户回放/夹具相关专项：**111 passed in 1.14s**，`/tmp/m44-targeted-final.log`（最后的移动围栏与样本断点改动随后纳入最终全量）。
- 第一轮全量（16 项新增测试，当时的实现）：**1696 passed in 213.70s**，`/tmp/m44-full.log`。
- 翻译 check：**1423 finished**，本轮 UI 文案保持现有源键。
- 最终全量（包含 21 项新回归及移动围栏/断点实现）：**1701 passed in 209.22s**，`/tmp/m44-full-final.log`。
- `git diff --check` 通过。最终专项同时检查 converter/TX/RX 三条温度历史，结果全部通过。

### 现场验证与源码运行

```bash
cd /Users/xmac/Documents/project_manager/1-softHz/1-project_upper/1-satlitecomm_debug_tool
.venv/bin/python -m satellite_debug_tool.main_user
# 运维版：.venv/bin/python -m satellite_debug_tool.main
```

现场步骤：持续接收 SNR 和模块温度，保持上位机连接并重启设备；观察新遥测连续推进后，当前读数、五分钟曲线、Y 范围及三模块温度同时更新。分别验证直接重启、清空显示后重启、切到其他页面再返回，以及长时间运行。真实设备回报频率、网络延迟和显示结果尚需现场确认。源码测试证明上位机状态与数据流合同；物理重启事实和现场稳定性独立验收。本轮执行源码验证，后续打包等待用户明确指令。
