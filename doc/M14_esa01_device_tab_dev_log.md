# M14 ESA01 Device Tab 开发记录

## 2026-07-14

- 确认上位机协议层已具备 `PROFILE_SEMANTICS`、参数表和 OTA 编解码能力。
- 计划补齐两处运行期行为：
  - `Handshake` 主动请求 profile semantics，但保持非阻塞兼容。
  - `DeviceView` 根据当前 `hw_type` 的 capability 启用参数/OTA，未知设备默认禁用。
- ESA01 固件侧同步接入参数/OTA 能力声明，详见 ESA01 仓库
  `src/modules/debug_report/plan.md` 的 Device Tab 扩展章节。

## 实施结果

- `Handshake` 已主动发送 `REQUEST_PROFILE_SEMANTICS`，ready 条件仍只依赖
  META/CHANNEL/STATE/EVENT 四类基础表。
- `DeviceView` 已复用 Live 的 `ProfileStore`，按 `parameters` / `ota`
  capability 启用参数和 OTA。
- ESA01 未声明 capability 时，Device Tab 不自动请求参数表；AFD01 保留旧兼容默认。
- 验证：
  - `PYTHONPATH=. pytest satellite_debug_tool/tests/test_handshake.py satellite_debug_tool/tests/test_device_view_capabilities.py -q`
    通过，15 passed。
  - `PYTHONPATH=. pytest satellite_debug_tool/tests -q` 通过，506 passed。

## 参数写入确认修复

- 现象：Device Tab 显示 `modem_baud=115200` 写入成功，但 ESA01 终端
  `fdb kv list` 仍为 `modem_baud=921600`。
- 根因：协议 `COMMAND_RESPONSE` 没有 request id / subcmd echo，DeviceView 原先会把
  pending 状态下收到的任意 `COMMAND_RESPONSE OK` 当成当前 `PARA_SET` 成功，存在假阳性。
- 修复：`PARA_SET` 现在必须重新读取 `PARA_TABLE_REPORT`，并确认目标参数读回值等于请求值后
  才显示 `✓ 成功`。普通 OK 只显示 `确认中...`；超时文案改为 `未读回目标值`。
- 验证：
  - `PYTHONPATH=. pytest satellite_debug_tool/tests/test_device_view_capabilities.py -q`
    通过，4 passed。

## 参数状态显示修复

- 现场复测日志显示 ESA01 已执行 `set_modem_baudrate: 115200`，并且后续
  `get_modem_baudrate` 读回为 `115200`，设备端 FDB 写入链路确认有效。
- 新问题：`PARA_SET` 成功后，DeviceView 会触发一次 200ms 重读和一次 500ms 重读；
  后到达的参数表会重建表格，把状态列清成空白，导致 UI 看起来没有成功状态。
- 修复：参数状态按参数名缓存；参数表刷新重建时回填已有状态。测试覆盖成功确认后再次刷新表格仍保持
  `✓ 成功`。
- 验证：
  - `PYTHONPATH=. pytest satellite_debug_tool/tests/test_device_view_capabilities.py -q`
    通过，4 passed。

## 参数读取节流与主动上报适配

- 现场现象：参数写入前后设备侧连续打印多轮 `get_*`，`PARA_SET` 延后才出现；
  刚连接后参数表也需要多次手动读取。
- 根因：上位机在 `PARA_SET` 发出后立即安排读表，收到 OK 后又安排读表；
  若旧读表请求还在链路中，会和写命令竞争，放大“配置生效慢”的体感。
- 修复：参数表读取增加 in-flight 防抖；写参数期间手动/自动读表会暂缓；
  仅在收到 `PARA_SET OK` 后发起读回确认，读回未匹配时再按 1s 节奏重试。
- 适配：新 ESA01 固件会在 `REQUEST_PROFILE_SEMANTICS` 握手响应后、以及 `DEBUG_ENABLE true`
  后主动发送一次 `PARA_TABLE_REPORT`；上位机若已收到主动参数表，会跳过 500ms 后的兼容自动读，
  减少连接阶段请求量。
- 验证：
  - `PYTHONPATH=. pytest satellite_debug_tool/tests/test_device_view_capabilities.py -q`
    通过，4 passed。

## 参数读取超时修复

- 现场现象：连接 ESA01 后 Device Tab 显示 `读取参数表超时`，表格为空。
- 根因：上位机新增的参数表读取超时为 3s；当前设备在 debug 实时数据流和控制请求并行时可能超过该时间。
  同时读取 pending 时手动点击 `读取全部` 被防抖挡住，无法主动重发请求。
- 修复：参数表读取超时调整为 10s；非 `PARA_SET` 写入确认期间，手动 `读取全部` 会强制重发
  `REQUEST_PARA_TABLE`。
- 验证：
  - `PYTHONPATH=. pytest satellite_debug_tool/tests/test_device_view_capabilities.py -q`
    通过，5 passed。

## Debug ON/OFF 观测修复

- 现场现象：点击 `Debug OFF` 后，界面仍像在刷新实时数据。
- 上位机复查：LiveView 的 `FPS` 统计在 `_update_display()` 定时刷新里自增，
  不是按 `DATA_REPORT` 到达计数；因此即使设备已停止实时帧，FPS 也会显示非 0。
- 修复：
  - `_frame_times` 只在收到 `DataReport` 时追加，`_update_display()` 只负责裁剪 1s 窗口并显示。
  - 复测发现本地 `Debug: OFF` 状态过滤 `DataReport` 会掩盖有效协议帧；最终改为收到
    `DataReport` 就更新曲线/FPS/FRM，是否发送实时数据只由设备端 `debug_mode` 控制。
- 联动：设备端已同步把 `debug_mode` 改为跨线程可见，并在 `Debug OFF` 时清理 pending 数据。
- 验证：
  - `PYTHONPATH=. pytest satellite_debug_tool/tests/test_dashboard_control_binding.py satellite_debug_tool/tests/test_device_view_capabilities.py -q`
    通过，9 passed。

## Debug ON/OFF ACK 修复

- 现场现象：连接后连续点 Debug ON/OFF，设备侧只出现参数表读取日志，偶尔最后一次才打印
  `debug mode on` 并开始曲线刷新。
- 根因：LiveView 原逻辑只要 UDP `send()` 成功就本地翻转按钮；UDP send 成功不代表设备已执行。
  如果第一条 ON 在握手/参数表流量中延迟或丢失，后续点击会和设备真实状态错位。
- 修复：
  - Debug 按钮改为发送明确目标状态后进入等待态，例如 `Debug: ON...`。
  - 只有收到设备专用 ACK `COMMAND_RESPONSE: DEBUG_ENABLE=1/0` 后才更新本地
    `_debug_enabled` 和按钮状态。
  - ACK 超时 800ms 自动重试，最多 2 次；仍失败则回退到上一次确认状态。
  - 普通 `OK` 不再被当成 Debug ACK，避免被握手/参数/其它控制响应误匹配。
- 验证：
  - 新增 `test_liveview_debug_waits_for_specific_ack`。
  - `PYTHONPATH=. pytest satellite_debug_tool/tests/test_dashboard_control_binding.py satellite_debug_tool/tests/test_device_view_capabilities.py satellite_debug_tool/tests/test_ux_batch_a.py -q`
    通过，19 passed。

## Debug 控制链路日志

- 现场现象：设备侧已打印 `debug mode on`，但上位机仍显示未确认并重试，需要判断 ACK 是未发出、
  未收到，还是收到后未匹配。
- 增加终端日志前缀 `[DBG_CTRL HH:MM:SS.mmm]`：
  - 发送 `DEBUG_ENABLE` 的目标状态和重试次数。
  - 等待 ACK 期间收到的 `COMMAND_RESPONSE code/msg`。
  - ACK 超时、最终失败、匹配成功。
  - 等待 ACK 期间或 Debug ON 后收到的第一帧 `DATA_REPORT`。
- 用法：直接从 `python3 -m satellite_debug_tool.main` 的终端输出判断链路阶段。

## Debug ACK 乱序容错

- 现场日志：上位机能收到 `COMMAND_RESPONSE`，但存在三类情况：
  - 请求前后出现 late `DEBUG_ENABLE=1/0`。
  - 等待 `DEBUG_ENABLE=1` 时先收到旧的 `DEBUG_ENABLE=0`。
  - 等待期间收到泛化 `OK`，无法确认属于哪条控制命令。
- 根因：当前协议的 `COMMAND_RESPONSE` 没有 request id/subcmd echo；UDP 链路和设备端发送队列里存在旧响应时，
  上位机不能把“第一条成功响应”直接当成当前 Debug 命令 ACK。
- 进一步确认：超时时设备端没有打印 `debug mode on/off`，只打印参数读取日志，说明
  `DEBUG_ENABLE` 不是已执行但 ACK 丢失，而是被前序参数表/FDB 读取阻塞在设备 RX 处理队列后面。
- 修复：
  - ACK 等待窗口调整为 15s，取消自动重试，避免连续重试制造更多旧响应堆积。
  - `OK` 继续作为非 Debug 响应忽略；反向 `DEBUG_ENABLE=0/1` 明确记录为 stale 并忽略。
  - 若等待 Debug ON 期间已收到 `DATA_REPORT`，说明设备端实时上报已生效，上位机直接确认 ON。
  - 若专用 ACK 在 pending 清除后 3s 内 late 到达，且匹配最近一次目标状态，则修正按钮状态。
- 验证：
  - 新增 `test_liveview_debug_data_report_confirms_on`。
  - 新增 `test_liveview_debug_accepts_recent_late_matching_ack`。
  - 新增 `test_liveview_debug_timeout_does_not_retry`。
  - `PYTHONPATH=. pytest satellite_debug_tool/tests/test_dashboard_control_binding.py satellite_debug_tool/tests/test_device_view_capabilities.py satellite_debug_tool/tests/test_ux_batch_a.py -q`
    通过，22 passed。

## State Panel 生命周期修复

- 现场现象：上位机运行时出现 `RuntimeError: libshiboken: Internal C++ object (StateItemRow) already deleted`。
- 根因：`StateItemRow.flash_highlight()` 使用 `QTimer.singleShot(..., lambda: self...)`；
  profile/state 重建会删除旧 row，但 singleShot 的 lambda 仍可能在 2 秒后访问已删除的 Qt 对象。
- 修复：改为 `StateItemRow` 自己持有 child `QTimer`，row 销毁时 timer 一起销毁，不再持有裸 `self`
  的延迟 lambda。
- 验证：新增 `test_highlight_timer_is_owned_by_row` 覆盖 timer 归属。

## 控制链路与可回退 OTA 实施启动

- 现场复核确认：ESA01 的 Debug 超时发生时，设备端连 `debug mode on/off` 日志都没有，
  说明命令尚未被 RX 控制路径执行；继续扩大上位机超时无法解决根因。
- 设备侧根因收敛为 RX 优先级过低、ringbuffer 信号量丢唤醒窗口、高频 report 任务持续抢占，
  以及 FDB/FAL/OTA 重操作与常数时间控制共用 RX 路径。
- 本阶段废止历史临时策略中的“15 秒 Debug 等待”和“设备端 1 秒同目标 ACK 去重”。
  新目标为每条命令 FIFO 执行并精确应答，上位机 3 秒超时且不自动重试。
- OTA 将从同步 socket 接管改为异步 Qt 状态机；Debug OTA 与 YMODEM 允许同版/降级，
  网络 OTA 保持严格升版。
- 实施前相关 pytest 基线：
  `test_handshake.py/test_dashboard_control_binding.py/test_device_view_capabilities.py/test_codec_v2.py`
  共 **59 passed**。
- 代码实施、构建结果和最终 plan 对照将在本节后续持续补记。

## 控制链路与异步 OTA 实施完成

- `Handshake` 首轮只发送 META/CHANNEL/STATE/EVENT；META 到达后立即请求 semantics，
  此后每 1 秒独立重试，总尝试次数最多 3 次。OTA 可暂停定义/semantics 重试，心跳计时不暂停。
- Live 页提供统一 Debug 控制接口：ON 允许首个 DATA_REPORT 辅助确认，OFF 只接受
  `DEBUG_ENABLE=0`；最终超时固定为 3 秒且不自动重试。
- Device OTA 已改为纯 Qt 事件状态机 `QUIESCE -> BEGIN -> DATA -> END -> WAIT_REBOOT`：
  - 不再访问 worker 私有 `_sock/_running`，不再重启 QThread，不再调用 `processEvents()`。
  - BEGIN/DATA/END 超时分别为 15s/2s/10s，DATA 最多重试 3 次，重新上线等待 120s。
  - 新 ESA01 严格匹配 `OTA_BEGIN=READY`、`OTA_DATA=<seq>`、`OTA_END=VERIFIED`；
    未声明 `command_response_context` 的旧 AFD01 继续接受通用成功响应。
  - WAIT_REBOOT 增加 1 秒旧 META 隔离窗，同版本镜像可由隔离窗后的 META 正确确认上线。
- 参数交互改为主动回表：semantics 后先等待 1.5 秒，未收到才兜底请求一次；手动/自动请求合并。
  新 ESA01 参数写入只接受 `PARA_SET=<name>`，最终以主动参数表读回值确认，不再每秒轮询。
- 自动化验证：
  - 相关握手/控制/Device/codec 测试由实施前 **59 passed** 扩展为 **69 passed**。
  - `PYTHONPATH=. pytest satellite_debug_tool/tests -q`：**524 passed, 4 warnings**。
  - 4 个 warning 均为原有 `datetime.utcnow()` 弃用提示，与本次功能无关。
- 尚需硬件验收：200 次 ON/OFF 延迟统计、实际同版/降级 OTA、错误镜像、120 秒重连、
  30 分钟实时流和 AFD01 实机兼容回归。

> 本节为 2026-07-14 控制链路的最终实现记录，取代前文“15 秒等待”、Debug 自动重试、
> 参数每秒轮询和仅允许高版本镜像等联调期临时方案。

## ESA01 等待握手但 DATA 正常的跨层定位

- 现场表现为 Live 已收到 16 路 DATA_REPORT，但通道名仍是 `ch_00...`，META、三张 DEFINE 和
  profile 均未就绪；这说明上位机只建立了 DataStore 占位通道，并非握手 UI 漏刷新。
- 独立 UDP 探针和 `tcpdump` 曾观察到 CONTROL 已离开 Mac、设备仍向旧端口发送 DATA，据此一度
  将根因归为 ESA01 WIZnet UDP RX；但后续复测发现更早版本握手正常，而网络层试验修改后故障范围
  反而扩大，因此该结论缺少设备内部阶段证据，现撤回为待验证假设。
- 新诊断版本恢复设备通用网络基线，并增加 `SATELLITE_DEBUG_LINK_TRACE=1`：记录 UdpWorker TX/RX、
  FrameReceiver 输出、Handshake 首发/重试/定义应用/ready 和 Debug ACK。DATA 仅限流汇总。
- 设备侧以 `debug_trace` 同步记录 LINK_RX、PARSE、DISPATCH、管理队列和 LINK_TX；只有 pcap 有请求
  而设备没有 LINK_RX 时，才重新调查 WIZnet/UDPServer。
- 上位机协议、握手超时、ready 条件和严格 ACK 均保持不变，本轮目标是一次上板测试锁定最后阶段，
  不在取证前追加推测性修复。

## 2026-07-15 双端追踪实施与第一轮证据

- 增加 `core/link_trace.py`，仅在 `SATELLITE_DEBUG_LINK_TRACE=1` 时输出日志；每行包含阶段、帧类型、
  CONTROL 子命令、长度和线上的 CRC 指纹，方便与设备日志跨时钟对照。
- `UdpWorker` 记录 CONNECT、TX_UDP、RX_UDP 和 socket 异常；`FrameReceiverV2` 记录 framing/CRC/payload
  错误及 DECODER_OUT 类型；Handshake 记录四类首发、缺失项重试、定义应用和 READY。
- LiveView 增加 `DBG_UI`，明确记录 Connect、Disconnect、Debug 点击及被忽略原因；Debug 请求状态机
  继续用 `DBG_CTRL` 记录目标值、发送结果、ACK、首个 DATA 辅助确认和超时。
- 既有 `DBG_CTRL` 纳入相同环境开关。DATA 在每个观察组件中只打印首帧和每秒汇总，其余业务逻辑、
  500 ms 定义重试、3 秒 Debug ACK 和 ready 条件未修改。
- 第一轮设备日志确认 Heartbeat PROTO_TX/LINK_TX 成功，但设备统计是在首次连接后才清零，且清零后
  没有重新 Connect 或点击 Debug；所以 LINK_RX/RX_FEED/PARSE 为 0 的窗口不具备 RX 诊断效力。
  下一轮应按规定顺序比较 `DBG_UI`、`DBG_HANDSHAKE TX`、`DBG_UDP TX_UDP` 与设备 `LINK_RX`。
- 新增 8 项追踪测试；全量结果为 `532 passed, 4 warnings`，warning 均为既有的
  `datetime.utcnow()` 弃用提示。

## 2026-07-15 第二轮双端证据与设备端根因

- 上位机持续实际发送四类请求，CRC 指纹为 `59CE/49EF/3908/2929`；设备日志逐一记录到相同 CRC 的
  `LINK_RX/RX_FEED/PARSE_OK/DISPATCH/CTL_ENQUEUE`，因此上位机 worker、Mac UDP 出站和设备网络接收
  均正常。
- 设备最终统计 `enqueue=41`、`dequeue=0`，且 Heartbeat 可持续反向到达上位机；故障明确位于
  新增的 `dbg_ctl` 消费逻辑，不是 Handshake/UI 漏刷新，也不是 WIZnet。
- RT-Thread 5.x 的 `rt_mq_recv()` 成功返回实际消息长度，设备代码却以 `== RT_EOK` 判断，导致消息
  出队后被静默丢弃。设备已改为完整长度校验并完成四组构建；上位机协议、超时和请求顺序无需修改。

## 2026-07-15 第三轮：握手恢复后 Debug 仍积压

- 新镜像已在约 1 秒内完成握手，并收到 semantics 和首次参数表，确认上位机状态机和设备管理队列
  均已恢复。
- `DBG_UI/DBG_UDP` 证明两次 Debug 点击都实际发送了 11 字节 `DEBUG_ENABLE=1`（CRC `DB72`）；
  设备无 ACK/DATA，后续手动参数请求也未返回，而上行 Heartbeat/定义持续正常。
- Debug 帧格式和 CRC 已逐字节核对一致。结合此前“点击一次只执行一个旧参数请求”的现场日志，固件
  进一步定位为 W5500 UDP 接收线程无法排空一次 RECV 中断下的多个数据报。
- 上位机本轮不改代码；保留精确 ACK 和 3 秒超时，让设备端修复后的行为可以被原验收条件直接验证。
- 设备 app/boot 的 Debug、Release 四组构建均通过；上位机
  `PYTHONPATH=. pytest satellite_debug_tool/tests -q` 为 `532 passed, 4 warnings`，warning 均为既有
  `datetime.utcnow()` 弃用提示。

## 2026-07-15 修复后实机闭环

- Debug OTA 实际发送 274 个 DATA 请求，每帧收到 `COMMAND_RESPONSE` 后状态机才推进；未出现 DATA
  超时或重试，END 成功后进入重启等待。
- 设备离线期间出现一次预期的 Heartbeat lost；约 15 秒后收到 `0.1.248` META，随后 Heartbeat 恢复，
  Device 参数表请求约 3 ms 返回。
- 设备日志确认两组 Debug ON/OFF 均按点击顺序立即执行；参数写入调用真实 FDB setter，
  `modem_baud=115200` 经设备 shell 查询确认持久化成功。
- 结论：上位机严格 ACK、异步 OTA 状态机与设备 W5500 排空修复已完成一次完整实机闭环。后续只保留
  200 次 ON/OFF、30 分钟持续运行及 OTA 异常/版本矩阵，不再为该问题增加超时或重试补丁。

## 2026-07-15 AFD01/ESA01 共用模型朝向修正

- 本地 `afd01.stl` 已复制为内容一致的 `esa01.stl`，两类设备继续按各自 `hw_type` 精确加载，不修改
  Profile 和设备识别逻辑。
- 真实模型原始包围盒为 `X=241、Y=179.5、Z=52.1`。旧配置把原始 `+X` 设为机头，导致机头端面
  是 179.5 的短边；按实物改为原始 `+Y` 机头。
- `normalize_mesh()` 增加 `left_sign` 参数，AFD01/ESA01 使用
  `+Y -> widget +X`、`-X -> widget +Y`、`+Z -> widget +Z`。该变换行列式为正，属于旋转而非镜像。
- 真实模型归一化后包围盒为 `X=2.681、Y=3.600、Z=0.778`，机头端面横跨 3.6 的长边。
- 新增轴向与右手系单测；STL/姿态相关测试 `46 passed`，全量测试
  `533 passed, 4 warnings`。warning 均为既有 `datetime.utcnow()` 弃用提示。
