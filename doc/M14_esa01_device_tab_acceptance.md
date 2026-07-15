# M14 ESA01 Device Tab 验收标准

## 上位机自动化

- `PYTHONPATH=. pytest satellite_debug_tool/tests -q` 通过。
- `test_handshake.py` 覆盖 `REQUEST_PROFILE_SEMANTICS` 发送和非阻塞 ready。
- Device Tab 测试覆盖：
  - 未连接时所有设备操作禁用。
  - 连接 ESA01 但无 capability 时，参数/OTA 禁用且不自动读参数。
  - 收到 `parameters=true`、`ota=true` 后，参数/OTA 启用。
  - AFD01 无 capability 时保持兼容启用。

## 硬件联调

- 旧 ESA01 固件连接后 Live 数据正常，Device Tab 显示当前固件未声明参数/OTA 支持。
- 新 ESA01 固件连接后，上位机收到 `PROFILE_SEMANTICS`，Device Tab 自动启用。
- 参数表可读，非法参数写入返回错误码且 UI 显示失败信息。
- OTA 错误 CRC 不重启；正确 CRC 且硬件型号匹配时，Debug OTA 允许同版/降级，网络 OTA 仍要求升版。

## 控制链路与 OTA 加固验收（2026-07-14）

- 握手首轮不发送 semantics；META 到达后独立每 1 秒重试，最多 3 次，缺失时仍可 ready。
- 连接新 ESA01 后 2 秒内参数表自动出现，正常握手只构造/发送一次参数表；1.5 秒内
  未主动收到时，上位机只发送一次兜底请求。
- 参数写入 1 秒内收到 `PARA_SET=<name>`，并由随后主动上报的参数表确认读回值。
- 连续交替 Debug ON/OFF 200 次，ACK p99 小于 200 ms、零超时；OFF 后 200 ms 内
  不再接收 DATA_REPORT。上位机 OFF 超时 3 秒且不自动重试。
- OTA 全程不访问 worker 的 `_sock/_running`，不重启通信线程，不调用
  `QApplication.processEvents()`。
- 新固件 OTA 严格匹配 BEGIN/DATA 序号/END 上下文 ACK；DATA 超时最多重试 3 次。
- Debug OTA 可完成同版本、降级和重新升级；错误 CRC、截断文件及异硬件镜像均被拒绝。
- AFD01 旧通用 ACK OTA 路径保持兼容；全量 pytest 通过。
- ESA01 在 50 Hz DATA 上报期间突发接收四个基础握手请求，500 ms 内返回 META、CHANNEL、
  STATE、EVENT；Live 不再停留在 `ch_00...` 占位通道和“等待设备握手”。
- 诊断日志默认关闭；设置 `SATELLITE_DEBUG_LINK_TRACE=1` 后，每个基础定义请求可从 UDP TX 追踪到
  decoder/Handshake，DATA_REPORT 日志按周期汇总而不逐帧刷屏。
- 与设备 `debug_trace` 和 pcap 联合采集后，一次连接、ON、OFF 测试能确定每条请求最后到达的阶段。
- 在证据出现前不放宽握手/Debug 超时，不将 WIZnet、`dbg_ctl` 或上位机状态机写成确定根因。
- 设备修复 W5500 UDP 排空后，首发四个握手请求、Debug ON/OFF 和手动参数读取均不得依赖后续请求
  触发；20 帧 CONTROL 突发应全部按发送顺序处理，上位机无 ACK 超时。

## 当前验收状态

| 项目 | 状态 | 结果 |
|---|---|---|
| 握手独立 semantics 重试 | 已通过 | 自动化覆盖首轮 4 请求、META 后请求、3 次上限、OTA 暂停 |
| 参数主动回表与请求合并 | 已通过 | 自动化覆盖主动表抑制兜底、pending 合并、上下文 ACK 与读回 |
| Debug 精确 ACK | 已通过 | 自动化覆盖普通 OK/反向 ACK 忽略、ON 数据辅助、OFF 必须精确 ACK |
| 异步 OTA 状态机 | 已通过 | 自动化覆盖上下文、序号、3 次重试、同版 META、旧 META 隔离 |
| 上位机全量回归 | 已通过 | `532 passed, 4 warnings`，warning 为既有 datetime 弃用提示 |
| ESA01/AFD01 实机与压力验收 | 待上板 | 需按本文件“硬件联调”和 200 次/30 分钟项目执行 |
| 握手回归分层取证 | 已定位/修复待复测 | CRC 对齐确认请求到达 CTL_ENQUEUE；设备 `rt_mq_recv()` 返回值误判已修复，上位机无需行为修改 |
| W5500 UDP 突发排空 | 实机通过 | Debug OTA 连续 274 个 DATA 逐帧收到 ACK，无旧包积压或额外请求唤醒 |

## 2026-07-15 实机复测补充

- Debug ON/OFF 两组均由设备立即执行并打印对应日志；本轮功能样本无 ACK 超时，200 次延迟统计仍待测。
- 参数表正常返回；`modem_baud=115200` 写入成功，设备 `fdb kv list` 确认实际持久化值为 `115200`。
- Debug OTA 连续 274 个 DATA 帧均完成请求/ACK 闭环，END 后设备重启，约 15 秒恢复 META，随后恢复
  Heartbeat；重启后参数表请求约 3 ms 返回。
- 本轮确认正常传输与重启路径，不替代同版/降级/重新升级、错误 CRC、截断、异硬件和断电恢复矩阵。

## 共用 3D 模型朝向验收

- `afd01.stl` 与 `esa01.stl` 内容一致，两类设备连接后均加载真实设备模型。
- 归一化后原始 `+Y` 指向 widget 机头 `+X`，原始 `+X` 指向 widget 右侧 `-Y`，三轴变换保持
  右手坐标系。
- 俯视时机头端面为 241 mm 长边，相对旧机头设定逆时针旋转 90 度；Roll/Pitch/Yaw 零位和波束
  世界方向不受影响。
- `test_stl_loader.py` 覆盖新轴向和非镜像约束，全量 pytest 通过。
